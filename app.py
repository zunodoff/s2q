from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
import requests
import json
import time
from datetime import datetime, timedelta
from collections import defaultdict
import logging
import os
import sqlite3
import hashlib
from urllib.parse import quote
from functools import wraps

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this-in-production')

# ===== ЛЕНИВАЯ ИНИЦИАЛИЗАЦИЯ =====
_db_initialized = False
_anime_service = None

def get_db_connection():
    """Получение соединения с БД с ленивой инициализацией"""
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True
    
    conn = sqlite3.connect('anivest.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect('anivest.db')
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) UNIQUE NOT NULL,
            email VARCHAR(100) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            avatar_url VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1,
            role VARCHAR(20) DEFAULT 'user'
        )
    ''')
    
    # Таблица комментариев
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_id VARCHAR(100) NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            is_spoiler BOOLEAN DEFAULT 0,
            rating INTEGER CHECK(rating >= 1 AND rating <= 10),
            episode_number INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            likes INTEGER DEFAULT 0,
            dislikes INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Остальные таблицы...
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS comment_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            vote_type VARCHAR(10) CHECK(vote_type IN ('like', 'dislike')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(comment_id, user_id),
            FOREIGN KEY (comment_id) REFERENCES comments (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    conn.close()

def get_anime_service():
    """Ленивая инициализация сервиса аниме"""
    global _anime_service
    if _anime_service is None:
        _anime_service = HybridAnimeService()
    return _anime_service

# ===== УПРОЩЕННЫЕ API КЛАССЫ =====

class ShikimoriAPI:
    def __init__(self):
        self.base_url = "https://shikimori.one/api"
        self.cache = {}
        self.cache_timeout = 600
        
    def _make_request(self, endpoint, params=None):
        """Запрос к Shikimori API с тайм-аутом"""
        cache_key = f"shiki_{endpoint}_{str(params)}"
        
        if cache_key in self.cache:
            cached_data, timestamp = self.cache[cache_key]
            if time.time() - timestamp < self.cache_timeout:
                return cached_data
        
        try:
            headers = {
                'User-Agent': 'Anivest/1.0',
                'Accept': 'application/json'
            }
            
            url = f"{self.base_url}/{endpoint}"
            response = requests.get(url, params=params or {}, headers=headers, timeout=5)
            response.raise_for_status()
            
            data = response.json()
            self.cache[cache_key] = (data, time.time())
            return data
            
        except Exception as e:
            logger.error(f"Shikimori API error: {e}")
            return []

    def search_anime(self, query=None, filters=None):
        """Поиск аниме"""
        params = {'limit': 20, 'censored': 'true'}
        if query:
            params['search'] = query
        return self._make_request('animes', params) or []

    def get_popular_anime(self, limit=20):
        """Популярные аниме"""
        params = {'limit': limit, 'order': 'popularity', 'censored': 'true'}
        return self._make_request('animes', params) or []

class KodikAPI:
    def __init__(self):
        self.base_url = "https://kodikapi.com"
        self.token = None
        self.cache = {}
        
    def get_token(self):
        """Получение токена без блокировки запуска"""
        if self.token:
            return self.token
            
        # Пробуем токены быстро
        test_tokens = ["447d179e875efe44217f20d1ee2146be"]
        for token in test_tokens:
            try:
                response = requests.post(f"{self.base_url}/list", 
                                       params={"token": token, "limit": 1}, timeout=3)
                if response.status_code == 200:
                    self.token = token
                    return token
            except:
                continue
        return None

    def search_by_shikimori_id(self, shikimori_id):
        """Поиск по ID с fallback"""
        if not self.token:
            self.get_token()
        if not self.token:
            return None
            
        try:
            params = {"token": self.token, "shikimori_id": shikimori_id, "limit": 1}
            response = requests.post(f"{self.base_url}/search", params=params, timeout=5)
            return response.json()
        except:
            return None

class HybridAnimeService:
    def __init__(self):
        self.shikimori = ShikimoriAPI()
        self.kodik = KodikAPI()
        
    def search_anime(self, query=None, filters=None):
        """Поиск аниме с fallback"""
        try:
            results = self.shikimori.search_anime(query, filters)
            return [self._convert_shikimori_format(anime) for anime in results[:12]]
        except:
            return []

    def get_popular_anime(self, limit=20):
        """Популярные аниме с fallback"""
        try:
            results = self.shikimori.get_popular_anime(limit)
            return [self._convert_shikimori_format(anime) for anime in results]
        except:
            return []

    def get_seasonal_anime(self, season=None, year=None, limit=20):
        """Сезонные аниме с fallback"""
        try:
            # Упрощенная версия - возвращаем популярные
            return self.get_popular_anime(limit)
        except:
            return []

    def _convert_shikimori_format(self, anime):
        """Конвертация формата"""
        return {
            'id': f"shiki_{anime.get('id', 0)}",
            'shikimori_id': anime.get('id'),
            'title': anime.get('russian') or anime.get('name', ''),
            'title_orig': anime.get('name', ''),
            'year': self._extract_year(anime.get('aired_on')),
            'material_data': {
                'title': anime.get('russian') or anime.get('name', ''),
                'poster_url': self._get_poster_url(anime),
                'description': anime.get('description', ''),
                'shikimori_rating': anime.get('score'),
                'anime_kind': anime.get('kind'),
                'anime_status': anime.get('status'),
                'anime_genres': [g.get('russian', g.get('name', '')) for g in anime.get('genres', [])[:3]],
                'episodes_total': anime.get('episodes')
            }
        }

    def _get_poster_url(self, anime):
        """Получение URL постера"""
        image = anime.get('image', {})
        if isinstance(image, dict) and image.get('original'):
            url = image['original']
            if url.startswith('/'):
                return f"https://shikimori.one{url}"
            return url
        return 'https://via.placeholder.com/300x400/8B5CF6/FFFFFF?text=Нет+постера'

    def _extract_year(self, date_string):
        """Извлечение года"""
        if date_string:
            try:
                return int(date_string.split('-')[0])
            except:
                pass
        return None

# ===== УТИЛИТЫ =====

def get_current_season():
    """Определение текущего сезона"""
    now = datetime.now()
    month = now.month
    year = now.year
    
    if month in [1, 2, 3]:
        return 'winter', year
    elif month in [4, 5, 6]:
        return 'spring', year
    elif month in [7, 8, 9]:
        return 'summer', year
    else:
        return 'fall', year

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Необходимо войти в систему', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    if 'user_id' not in session:
        return None
    
    try:
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        conn.close()
        return dict(user) if user else None
    except:
        return None

# ===== РОУТЫ =====

@app.route('/')
def index():
    """Главная страница с обработкой ошибок"""
    try:
        current_season, current_year = get_current_season()
        anime_service = get_anime_service()
        
        # Получаем данные с fallback
        seasonal_anime = anime_service.get_seasonal_anime(current_season, current_year, 6)
        popular_anime = anime_service.get_popular_anime(12)
        
        return render_template('index.html', 
                             seasonal_anime=seasonal_anime,
                             popular_anime=popular_anime,
                             current_season=current_season,
                             current_year=current_year,
                             season_name_ru='летнего',
                             season_emoji='☀️',
                             current_user=get_current_user())
    except Exception as e:
        logger.error(f"Index error: {e}")
        return render_template('index.html', 
                             seasonal_anime=[], 
                             popular_anime=[],
                             current_season='summer',
                             current_year=2025,
                             season_name_ru='летнего',
                             season_emoji='☀️',
                             current_user=get_current_user())

@app.route('/catalog')
def catalog():
    """Каталог аниме"""
    try:
        query = request.args.get('q', '').strip()
        anime_service = get_anime_service()
        
        if query:
            anime_list = anime_service.search_anime(query)
        else:
            anime_list = anime_service.get_popular_anime(20)
            
        return render_template('catalog.html', 
                             anime_list=anime_list,
                             query=query,
                             filters=request.args,
                             current_user=get_current_user())
    except Exception as e:
        logger.error(f"Catalog error: {e}")
        return render_template('catalog.html', 
                             anime_list=[], 
                             query=query,
                             filters=request.args,
                             current_user=get_current_user())

@app.route('/watch/<anime_id>')
def watch(anime_id):
    """Страница просмотра"""
    try:
        # Заглушка для страницы просмотра
        anime = {
            'id': anime_id,
            'title': 'Тестовое аниме',
            'material_data': {
                'poster_url': 'https://via.placeholder.com/300x400/8B5CF6/FFFFFF?text=Тест',
                'description': 'Описание тестового аниме'
            }
        }
        return render_template('watch.html', anime=anime, current_user=get_current_user())
    except Exception as e:
        logger.error(f"Watch error: {e}")
        return render_template('error.html', message="Аниме не найдено"), 404

@app.route('/subscription')
def subscription():
    """Страница подписки"""
    return render_template('subscription.html', current_user=get_current_user())

# ===== API РОУТЫ =====

@app.route('/api/search')
def api_search():
    """API поиска"""
    try:
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({"error": "Пустой запрос"}), 400
            
        anime_service = get_anime_service()
        results = anime_service.search_anime(query)
        return jsonify({"results": results})
    except Exception as e:
        logger.error(f"Search API error: {e}")
        return jsonify({"error": "Ошибка поиска"}), 500

@app.route('/health')
def health_check():
    """Проверка здоровья"""
    return jsonify({
        "status": "OK",
        "timestamp": datetime.now().isoformat()
    })

# ===== АВТОРИЗАЦИЯ (упрощенно) =====

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Простая заглушка для логина
        flash('Демо-режим: вход выполнен', 'success')
        session['user_id'] = 1
        session['username'] = 'demo_user'
        return redirect(url_for('index'))
    return render_template('auth/login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        flash('Демо-режим: регистрация выполнена', 'success')
        return redirect(url_for('login'))
    return render_template('auth/register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('index'))

# ===== ЮРИДИЧЕСКИЕ СТРАНИЦЫ =====

@app.route('/terms-of-service')
def terms_of_service():
    return render_template('legal/terms.html', current_user=get_current_user())

@app.route('/privacy-policy') 
def privacy_policy():
    return render_template('legal/privacy.html', current_user=get_current_user())

@app.route('/cookie-policy')
def cookie_policy():
    return render_template('legal/cookies.html', current_user=get_current_user())

@app.route('/dmca')
def dmca():
    return render_template('legal/dmca.html', current_user=get_current_user())

# ===== ЗАПУСК =====

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
