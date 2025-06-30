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
app.secret_key = 'your-secret-key-change-this-in-production'  # Измените в продакшене!

# ===== НАСТРОЙКА БАЗЫ ДАННЫХ =====
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
    
    # Таблица лайков/дизлайков комментариев
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
    
    # Таблица избранного аниме
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            anime_id VARCHAR(100) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, anime_id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Таблица истории просмотров
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            anime_id VARCHAR(100) NOT NULL,
            episode_number INTEGER DEFAULT 1,
            season_number INTEGER DEFAULT 1,
            last_watched TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, anime_id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    conn.close()

# Инициализируем БД при запуске
init_db()

def get_db_connection():
    """Получение соединения с БД"""
    conn = sqlite3.connect('anivest.db')
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password):
    """Хеширование пароля"""
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    """Декоратор для защищенных страниц"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Необходимо войти в систему', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    """Получение текущего пользователя"""
    if 'user_id' not in session:
        return None
    
    conn = get_db_connection()
    user = conn.execute(
        'SELECT * FROM users WHERE id = ?', (session['user_id'],)
    ).fetchone()
    conn.close()
    
    return dict(user) if user else None

# ===== ОПРЕДЕЛЕНИЕ СЕЗОНОВ =====

def get_current_season():
    """Определение текущего сезона аниме"""
    now = datetime.now()
    month = now.month
    year = now.year
    
    # Определяем сезон по месяцу
    if month in [1, 2, 3]:  # Зима: декабрь, январь, февраль
        season = 'winter'
        # Если декабрь, то это зима следующего года
        if month == 12:
            year += 1
    elif month in [4, 5, 6]:  # Весна: март, апрель, май
        season = 'spring'
    elif month in [7, 8, 9]:  # Лето: июнь, июль, август
        season = 'summer'
    else:  # Осень: сентябрь, октябрь, ноябрь
        season = 'fall'
    
    return season, year

def get_season_name_ru(season):
    """Получение русского названия сезона"""
    season_names = {
        'winter': 'зимнего',
        'spring': 'весеннего', 
        'summer': 'летнего',
        'fall': 'осеннего'
    }
    return season_names.get(season, 'текущего')

def get_season_emoji(season):
    """Получение эмодзи для сезона"""
    season_emojis = {
        'winter': '❄️',
        'spring': '🌸',
        'summer': '☀️', 
        'fall': '🍂'
    }
    return season_emojis.get(season, '🌟')

# ===== API КЛАССЫ =====

class ShikimoriAPI:
    def __init__(self):
        self.base_url = "https://shikimori.one/api"
        self.cache = {}
        self.cache_timeout = 600  # 10 минут
        
    def _make_request(self, endpoint, params=None):
        """Базовый метод для выполнения запросов к Shikimori API"""
        cache_key = f"shiki_{endpoint}_{str(params)}"
        
        # Проверяем кеш
        if cache_key in self.cache:
            cached_data, timestamp = self.cache[cache_key]
            if time.time() - timestamp < self.cache_timeout:
                logger.info(f"Shikimori cache hit: {endpoint}")
                return cached_data
        
        try:
            headers = {
                'User-Agent': 'Anivest/1.0 (https://anivest.local)',
                'Accept': 'application/json'
            }
            
            full_params = {}
            if params:
                full_params.update(params)
                
            url = f"{self.base_url}/{endpoint}"
            logger.info(f"Shikimori request: {url} with params: {full_params}")
            
            response = requests.get(url, params=full_params, headers=headers, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"Shikimori response: {len(data) if isinstance(data, list) else 1} items")
            
            # Логируем первый элемент для диагностики
            if isinstance(data, list) and data:
                first_item = data[0]
                logger.info(f"Первый элемент: {first_item.get('name')} [{first_item.get('kind')}] - рейтинг: {first_item.get('score')}")
            
            # Кешируем результат
            self.cache[cache_key] = (data, time.time())
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка Shikimori API запроса: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON от Shikimori: {e}")
            return None

    def search_anime(self, query=None, filters=None):
        """Поиск аниме в Shikimori"""
        params = {
            'limit': 50,
            'censored': 'true'  # Скрываем 18+ контент
        }
        
        if query:
            params['search'] = query
            
        if filters:
            # Маппинг наших фильтров на Shikimori параметры
            if filters.get('genre'):
                # Русские жанры → ID жанров для Shikimori
                genre_mapping = {
                    'Экшен': '1',          # Action
                    'Приключения': '2',    # Adventure
                    'Комедия': '4',        # Comedy
                    'Драма': '8',          # Drama
                    'Фэнтези': '10',       # Fantasy
                    'Романтика': '22',     # Romance
                    'Фантастика': '24',    # Sci-Fi
                    'Сверхъестественное': '37',  # Supernatural
                    'Психологическое': '40',     # Psychological
                    'Триллер': '41',       # Thriller
                    'Повседневность': '36', # Slice of Life
                    'Школа': '23',         # School
                    'Спорт': '30',         # Sports
                    'Военное': '38',       # Military
                    'Исторический': '13'   # Historical
                }
                genre = filters['genre']
                mapped_genre = genre_mapping.get(genre)
                if mapped_genre:
                    params['genre'] = mapped_genre
                    logger.info(f"Маппинг жанра: {genre} → ID {mapped_genre}")
                    
            if filters.get('type'):
                # Маппинг типов
                type_mapping = {
                    'tv': 'tv',
                    'movie': 'movie', 
                    'ova': 'ova',
                    'ona': 'ona',
                    'special': 'special'
                }
                filter_type = filters['type']
                if filter_type in type_mapping:
                    params['kind'] = type_mapping[filter_type]
                    
            if filters.get('status'):
                # Маппинг статусов
                status_mapping = {
                    'released': 'released',
                    'ongoing': 'ongoing', 
                    'anons': 'anons'
                }
                if filters['status'] in status_mapping:
                    params['status'] = status_mapping[filters['status']]
            
            # Обработка сезонного фильтра
            if filters.get('season'):
                # Формат: "summer_2025"
                params['season'] = filters['season']
                logger.info(f"Фильтр по сезону: {filters['season']}")
                    
            # Обработка диапазона годов
            elif filters.get('year_from') and filters.get('year_to'):
                year_from = filters['year_from']
                year_to = filters['year_to']
                
                # Если года одинаковые, используем season для конкретного года
                if year_from == year_to:
                    params['season'] = str(year_from)
                    logger.info(f"Фильтр по году: {year_from}")
            elif filters.get('year_from'):
                # Только начальный год
                params['season'] = f"{filters['year_from']}"
            elif filters.get('year_to'):
                # Только конечный год
                params['season'] = f"{filters['year_to']}"
            elif filters.get('year'):
                # Старый параметр year для совместимости
                params['season'] = f"{filters['year']}"
                    
        # Сортировка по популярности по умолчанию
        if 'order' not in params:
            params['order'] = 'popularity'
        
        logger.info(f"Поиск аниме: query='{query}', params={params}")
        return self._make_request('animes', params)

    def get_anime(self, anime_id):
        """Получение информации об одном аниме"""
        return self._make_request(f'animes/{anime_id}')

    def get_seasonal_anime(self, season=None, year=None, limit=20):
        """Получение популярных аниме текущего/указанного сезона"""
        if not season or not year:
            # Если сезон не указан, получаем текущий
            season, year = get_current_season()
        
        # Сначала пробуем строгие параметры
        params = {
            'limit': limit * 3,  # Запрашиваем больше для фильтрации
            'season': f'{season}_{year}',
            'order': 'popularity',
            'censored': 'true',
            'status': 'released,ongoing'  # Только вышедшие и онгоинги
        }
        
        logger.info(f"Запрос сезонных аниме: {season}_{year}")
        
        # Получаем данные от Shikimori
        all_results = self._make_request('animes', params)
        
        if not all_results:
            logger.warning("Не удалось получить сезонные аниме, пробуем без фильтров сезона")
            # Fallback: получаем популярные аниме этого года
            fallback_params = {
                'limit': limit * 2,
                'order': 'popularity',
                'censored': 'true',
                'season': str(year)
            }
            all_results = self._make_request('animes', fallback_params)
        
        if not all_results:
            logger.warning("Fallback тоже не сработал")
            return []
        
        # Фильтрация на стороне клиента для получения только аниме
        filtered_results = []
        for anime in all_results:
            anime_kind = anime.get('kind', '')
            anime_score = float(anime.get('score', 0) or 0)
            scored_by = int(anime.get('scored_by', 0) or 0)
            
            # ✅ Более мягкие фильтры для аниме:
            if (anime_kind in ['tv', 'movie', 'ova', 'ona', 'special'] and  # Только аниме
                (anime_score >= 5.0 or scored_by >= 500)):  # Либо хороший рейтинг, либо популярность
                
                # Добавляем показатель популярности для сортировки
                anime['popularity_score'] = (scored_by * 0.1) + (anime_score * 1000)
                filtered_results.append(anime)
        
        # Сортируем по популярности
        filtered_results.sort(key=lambda x: x.get('popularity_score', 0), reverse=True)
        
        # Возвращаем топ аниме
        top_results = filtered_results[:limit]
        
        logger.info(f"Возвращаем {len(top_results)} сезонных аниме для {season}_{year}")
        
        # Логируем топ-3 для проверки
        for i, anime in enumerate(top_results[:3], 1):
            score = anime.get('score', 'N/A')
            scored_by = anime.get('scored_by', 'N/A')
            kind = anime.get('kind', 'N/A')
            logger.info(f"#{i}: {anime.get('name')} [{kind}] - Рейтинг: {score}, Оценок: {scored_by}")
        
        return top_results

    def get_popular_anime(self, limit=20):
        """Получение популярных аниме всех времён"""
        params = {
            'limit': limit * 2,  # Запрашиваем больше для фильтрации
            'order': 'popularity',
            'censored': 'true'
        }
        
        logger.info(f"Запрос {limit} популярных аниме")
        
        # Получаем данные от Shikimori
        all_results = self._make_request('animes', params)
        
        if not all_results:
            return []
        
        # Фильтрация популярных аниме
        filtered_results = []
        for anime in all_results:
            anime_kind = anime.get('kind', '')
            anime_score = float(anime.get('score', 0) or 0)
            scored_by = int(anime.get('scored_by', 0) or 0)
            
            # ✅ Более мягкие фильтры для популярных аниме:
            if (anime_kind in ['tv', 'movie', 'ova', 'ona', 'special'] and  # Только аниме
                (anime_score >= 6.0 or scored_by >= 1000)):  # Либо хороший рейтинг, либо много оценок
                
                filtered_results.append(anime)
        
        # Сортируем по рейтингу и популярности
        filtered_results.sort(key=lambda x: (float(x.get('score', 0) or 0), int(x.get('scored_by', 0) or 0)), reverse=True)
        
        # Возвращаем топ популярных аниме
        top_results = filtered_results[:limit]
        
        logger.info(f"Возвращаем {len(top_results)} популярных аниме")
        
        return top_results

class KodikAPI:
    def __init__(self):
        self.base_url = "https://kodikapi.com"
        self.token = None
        self.cache = {}
        self.cache_timeout = 300  # 5 минут
        
    def get_token(self):
        """Получение токена для API"""
        test_tokens = [
            "447d179e875efe44217f20d1ee2146be",
            "73f8b92d87eb24e1a95e64e9d33d0a34",
            "2f84e5c78ba6a64f7e3d91c45b0c12aa"
        ]
        
        for token in test_tokens:
            try:
                response = requests.post(f"{self.base_url}/list", 
                                       params={"token": token, "limit": 1})
                if response.status_code == 200:
                    self.token = token
                    logger.info(f"Kodik токен найден: {token[:10]}...")
                    return token
            except Exception as e:
                logger.error(f"Ошибка при проверке Kodik токена {token}: {e}")
                continue
        
        logger.warning("Не удалось найти рабочий Kodik токен!")
        return None

    def _make_request(self, endpoint, params=None):
        """Базовый метод для выполнения запросов к Kodik API"""
        if not self.token:
            self.get_token()
            
        if not self.token:
            logger.error("Kodik токен недоступен")
            return None
            
        cache_key = f"kodik_{endpoint}_{str(params)}"
        
        # Проверяем кеш
        if cache_key in self.cache:
            cached_data, timestamp = self.cache[cache_key]
            if time.time() - timestamp < self.cache_timeout:
                return cached_data
        
        try:
            full_params = {"token": self.token}
            if params:
                full_params.update(params)
                
            url = f"{self.base_url}/{endpoint}"
            logger.info(f"Kodik request: {url}")
            
            response = requests.post(url, params=full_params, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            # Кешируем результат
            self.cache[cache_key] = (data, time.time())
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка Kodik API запроса: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON от Kodik: {e}")
            return None

    def search_by_shikimori_id(self, shikimori_id):
        """Поиск в Kodik по shikimori_id"""
        params = {
            "shikimori_id": shikimori_id,
            "with_material_data": True,
            "limit": 20
        }
        return self._make_request("search", params)

    def search_by_title(self, title):
        """Поиск в Kodik по названию"""
        params = {
            "title": title,
            "with_material_data": True,
            "limit": 10
        }
        return self._make_request("search", params)

class HybridAnimeService:
    """Гибридный сервис, использующий Shikimori для поиска и Kodik для плеера"""
    
    def __init__(self):
        self.shikimori = ShikimoriAPI()
        self.kodik = KodikAPI()
        self.poster_cache = {}  # Кеш проверок доступности постеров
        
    def _check_image_availability(self, url, timeout=3):
        """Проверка доступности изображения по URL"""
        if not url:
            return False
            
        # Проверяем кеш
        if url in self.poster_cache:
            return self.poster_cache[url]
        
        try:
            # Делаем HEAD запрос для проверки без загрузки всего изображения
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            
            # Проверяем статус код и тип контента
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '').lower()
                # Проверяем, что это действительно изображение
                if any(img_type in content_type for img_type in ['image/', 'jpeg', 'png', 'gif', 'webp']):
                    self.poster_cache[url] = True
                    return True
            
            logger.warning(f"Изображение недоступно: {url} (статус: {response.status_code})")
            self.poster_cache[url] = False
            return False
            
        except Exception as e:
            logger.warning(f"Ошибка при проверке изображения {url}: {e}")
            self.poster_cache[url] = False
            return False
        
    def search_anime(self, query=None, filters=None):
        """Основной поиск аниме"""
        try:
            # 1. Ищем в Shikimori (основной поиск)
            logger.info(f"Поиск в Shikimori: query='{query}', filters={filters}")
            shikimori_results = self.shikimori.search_anime(query, filters)
            
            if not shikimori_results:
                logger.warning("Shikimori не вернул результатов")
                return []
            
            # 2. Фильтрация на стороне клиента для исключения дорам
            anime_results = []
            for anime in shikimori_results:
                anime_kind = anime.get('kind', '')
                # Исключаем дорамы, но оставляем все виды аниме
                if anime_kind in ['tv', 'movie', 'ova', 'ona', 'special', 'music']:
                    anime_results.append(anime)
                else:
                    logger.debug(f"Исключаем не-аниме: {anime.get('name')} [{anime_kind}]")
            
            # 3. Фильтрация по диапазону годов на стороне клиента (если нужно)
            if filters and filters.get('year_from') and filters.get('year_to'):
                year_from = int(filters['year_from'])
                year_to = int(filters['year_to'])
                
                if year_from != year_to:
                    year_filtered_results = []
                    for anime in anime_results:
                        anime_year = self._extract_year(anime.get('aired_on'))
                        if anime_year and year_from <= anime_year <= year_to:
                            year_filtered_results.append(anime)
                    anime_results = year_filtered_results
                    logger.info(f"После фильтрации по годам {year_from}-{year_to}: {len(anime_results)} аниме")
            
            # 4. Обогащаем данными из Kodik (плееры)
            enriched_results = []
            for anime in anime_results[:20]:  # Ограничиваем для производительности
                enriched_anime = self._enrich_with_kodik(anime)
                enriched_results.append(enriched_anime)
                
            logger.info(f"Найдено {len(enriched_results)} аниме (исключены дорамы)")
            return enriched_results
            
        except Exception as e:
            logger.error(f"Ошибка при поиске аниме: {e}")
            return []

    def get_seasonal_anime(self, season=None, year=None, limit=20):
        """Получение аниме текущего/указанного сезона"""
        try:
            logger.info(f"Получение сезонных аниме: {season}_{year} (лимит: {limit})")
            shikimori_results = self.shikimori.get_seasonal_anime(season, year, limit)
            
            if not shikimori_results:
                logger.warning("Shikimori не вернул сезонных аниме")
                # Fallback: получаем просто популярные аниме
                logger.info("Используем fallback: популярные аниме")
                return self.get_popular_anime(limit)
            
            # Обогащаем данными из Kodik
            enriched_results = []
            for anime in shikimori_results:
                enriched_anime = self._enrich_with_kodik(anime)
                enriched_results.append(enriched_anime)
                
            logger.info(f"Найдено {len(enriched_results)} сезонных аниме")
            return enriched_results
            
        except Exception as e:
            logger.error(f"Ошибка при получении сезонных аниме: {e}")
            # В случае ошибки возвращаем популярные аниме
            logger.info("Ошибка, используем fallback: популярные аниме")
            return self.get_popular_anime(limit)

    def get_popular_anime(self, limit=20):
        """Получение популярных аниме"""
        try:
            logger.info(f"Получение {limit} популярных аниме")
            shikimori_results = self.shikimori.get_popular_anime(limit)
            
            if not shikimori_results:
                logger.warning("Не удалось получить популярные аниме")
                return []
            
            enriched_results = []
            for anime in shikimori_results:
                enriched_anime = self._enrich_with_kodik(anime)
                enriched_results.append(enriched_anime)
                
            logger.info(f"Найдено {len(enriched_results)} популярных аниме")
            return enriched_results
            
        except Exception as e:
            logger.error(f"Ошибка при получении популярных аниме: {e}")
            return []

    def get_anime_details(self, anime_id, shikimori_id=None):
        """Получение детальной информации об аниме"""
        try:
            # Если есть shikimori_id, используем его для получения полной информации
            if shikimori_id:
                logger.info(f"Получение деталей аниме из Shikimori: {shikimori_id}")
                shikimori_anime = self.shikimori.get_anime(shikimori_id)
                if shikimori_anime:
                    enriched = self._enrich_with_kodik(shikimori_anime)
                    return enriched
            
            # Fallback: поиск в Kodik по ID
            logger.info(f"Fallback поиск в Kodik: {anime_id}")
            kodik_results = self.kodik.search_by_title("")  # Получаем любые результаты
            if kodik_results and 'results' in kodik_results:
                for anime in kodik_results['results']:
                    if anime.get('id') == anime_id:
                        return anime
                        
            return None
            
        except Exception as e:
            logger.error(f"Ошибка при получении деталей аниме: {e}")
            return None

    def _enrich_with_kodik(self, shikimori_anime):
        """Обогащение аниме из Shikimori данными из Kodik"""
        try:
            # Ищем в Kodik по shikimori_id
            kodik_results = self.kodik.search_by_shikimori_id(shikimori_anime['id'])
            
            # Если не нашли по ID, пробуем по названию
            if not kodik_results or not kodik_results.get('results'):
                title = shikimori_anime.get('russian') or shikimori_anime.get('name', '')
                if title:
                    kodik_results = self.kodik.search_by_title(title)
            
            # Объединяем данные
            enriched = self._merge_anime_data(shikimori_anime, kodik_results)
            
            return enriched
            
        except Exception as e:
            logger.error(f"Ошибка при обогащении аниме {shikimori_anime.get('id')}: {e}")
            # Возвращаем только данные Shikimori
            return self._convert_shikimori_format(shikimori_anime)

    def _merge_anime_data(self, shikimori_anime, kodik_results):
        """Объединение данных из Shikimori и Kodik"""
        # Начинаем с данных Shikimori
        merged = self._convert_shikimori_format(shikimori_anime)
        
        # Добавляем данные Kodik если есть
        if kodik_results and kodik_results.get('results'):
            kodik_anime = kodik_results['results'][0]  # Берем первый результат
            
            # Добавляем важные поля из Kodik
            merged.update({
                'kodik_id': kodik_anime.get('id'),
                'link': kodik_anime.get('link'),
                'translation': kodik_anime.get('translation'),
                'quality': kodik_anime.get('quality'),
                'episodes_count': kodik_anime.get('episodes_count') or merged.get('episodes_count'),
                'seasons': kodik_anime.get('seasons'),
                'screenshots': kodik_anime.get('screenshots', [])
            })
            
            # *** ИСПРАВЛЕНИЕ: Улучшенная проверка постеров ***
            shikimori_poster = merged.get('material_data', {}).get('poster_url', '')
            kodik_material = kodik_anime.get('material_data', {})
            kodik_poster = kodik_material.get('poster_url', '')
            
            # Проверяем, нужно ли заменить постер Shikimori на Kodik
            should_replace_poster = (
                # Если постер - placeholder
                shikimori_poster.startswith('https://via.placeholder.com') or
                # Если в URL есть "404" (индикатор недоступности)
                '404' in shikimori_poster.lower() or
                # Если постер пустой
                not shikimori_poster or
                # Если URL содержит типичные индикаторы ошибок
                any(error_indicator in shikimori_poster.lower() 
                    for error_indicator in ['not_found', 'notfound', 'error', 'missing', 'no+image'])
            )
            
            if should_replace_poster and kodik_poster:
                logger.info(f"Заменяем проблемный постер Shikimori на Kodik для аниме {shikimori_anime.get('id')}")
                logger.info(f"Старый URL: {shikimori_poster}")
                logger.info(f"Новый URL: {kodik_poster}")
                
                merged['material_data']['poster_url'] = kodik_poster
                merged['material_data']['anime_poster_url'] = kodik_poster
            
            # Если постер выглядит нормально, но все же может быть недоступен, проверяем доступность
            elif shikimori_poster and not shikimori_poster.startswith('https://via.placeholder.com'):
                if not self._check_image_availability(shikimori_poster):
                    if kodik_poster and self._check_image_availability(kodik_poster):
                        logger.info(f"HTTP проверка: заменяем недоступный постер Shikimori на Kodik для аниме {shikimori_anime.get('id')}")
                        merged['material_data']['poster_url'] = kodik_poster
                        merged['material_data']['anime_poster_url'] = kodik_poster
                    else:
                        logger.warning(f"Постер Shikimori недоступен, Kodik также не подходит: {shikimori_anime.get('id')}")
                        merged['material_data']['poster_url'] = 'https://via.placeholder.com/300x400/8B5CF6/FFFFFF?text=Нет+постера'
                        merged['material_data']['anime_poster_url'] = 'https://via.placeholder.com/300x400/8B5CF6/FFFFFF?text=Нет+постера'
        
        return merged

    def _convert_shikimori_format(self, shikimori_anime):
        """Конвертация формата Shikimori в наш формат"""
        # Мапим поля Shikimori на наш формат
        converted = {
            'id': f"shiki_{shikimori_anime['id']}",  # Префикс для различения
            'shikimori_id': shikimori_anime['id'],
            'title': shikimori_anime.get('russian') or shikimori_anime.get('name', ''),
            'title_orig': shikimori_anime.get('name', ''),
            'other_title': shikimori_anime.get('synonyms', []),
            'year': None,
            'type': 'anime',
            'link': None,  # Будет заполнено из Kodik
            'kodik_id': None,
            'translation': None,
            'quality': None,
            'episodes_count': shikimori_anime.get('episodes'),
            'last_episode': None,
            'seasons': None,
            'screenshots': [],
            
            # Материальные данные (совместимость с шаблонами)
            'material_data': {
                'title': shikimori_anime.get('russian') or shikimori_anime.get('name', ''),
                'title_en': shikimori_anime.get('name', ''),
                'anime_title': shikimori_anime.get('russian') or shikimori_anime.get('name', ''),
                'description': shikimori_anime.get('description', ''),
                'anime_description': shikimori_anime.get('description', ''),
                'poster_url': self._get_poster_url(shikimori_anime),
                'anime_poster_url': self._get_poster_url(shikimori_anime),
                'shikimori_rating': shikimori_anime.get('score'),
                'shikimori_votes': shikimori_anime.get('scored_by'),
                'anime_kind': shikimori_anime.get('kind'),
                'anime_status': shikimori_anime.get('status'),
                'all_status': shikimori_anime.get('status'),
                'anime_genres': [g['russian'] for g in shikimori_anime.get('genres', []) if g.get('russian')],
                'all_genres': [g['russian'] for g in shikimori_anime.get('genres', []) if g.get('russian')],
                'episodes_total': shikimori_anime.get('episodes'),
                'episodes_aired': shikimori_anime.get('episodes_aired'),
                'anime_studios': [s['name'] for s in shikimori_anime.get('studios', [])],
                'rating_mpaa': shikimori_anime.get('rating'),
                'aired_at': shikimori_anime.get('aired_on'),
                'released_at': shikimori_anime.get('released_on'),
                'year': self._extract_year(shikimori_anime.get('aired_on'))
            }
        }
        
        # Устанавливаем год
        if converted['material_data']['year']:
            converted['year'] = converted['material_data']['year']
            
        return converted

    def _get_poster_url(self, shikimori_anime):
        """Получение URL постера из Shikimori с улучшенной обработкой ошибок"""
        image = shikimori_anime.get('image', {})
        
        if isinstance(image, dict):
            # Получаем URL и добавляем домен Shikimori если нужно
            poster_url = None
            
            # Пробуем разные размеры (от большего к меньшему)
            for size in ['original', 'preview', 'x96', 'x48']:
                if image.get(size):
                    poster_url = image[size]
                    break
            
            if poster_url:
                # Если URL относительный, добавляем домен Shikimori
                if poster_url.startswith('/'):
                    poster_url = f"https://shikimori.one{poster_url}"
                elif not poster_url.startswith('http'):
                    poster_url = f"https://shikimori.one{poster_url}"
                
                # *** ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: если URL содержит индикаторы ошибок ***
                if any(error_indicator in poster_url.lower() 
                       for error_indicator in ['404', 'not_found', 'notfound', 'error', 'missing', 'no+image']):
                    logger.warning(f"Обнаружен проблемный URL постера: {poster_url}")
                    return 'https://via.placeholder.com/300x400/8B5CF6/FFFFFF?text=Нет+постера'
                
                return poster_url
        
        # Fallback постер
        return 'https://via.placeholder.com/300x400/8B5CF6/FFFFFF?text=Нет+постера'

    def _extract_year(self, date_string):
        """Извлечение года из даты"""
        if date_string:
            try:
                return int(date_string.split('-')[0])
            except:
                pass
        return None

# Инициализация сервиса
anime_service = HybridAnimeService()

# ===== РОУТЫ АВТОРИЗАЦИИ =====

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Регистрация пользователя"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Валидация
        if not username or len(username) < 3:
            flash('Имя пользователя должно содержать минимум 3 символа', 'error')
            return render_template('auth/register.html')
            
        if not email or '@' not in email:
            flash('Введите корректный email', 'error')
            return render_template('auth/register.html')
            
        if not password or len(password) < 6:
            flash('Пароль должен содержать минимум 6 символов', 'error')
            return render_template('auth/register.html')
            
        if password != confirm_password:
            flash('Пароли не совпадают', 'error')
            return render_template('auth/register.html')
        
        # Проверяем уникальность
        conn = get_db_connection()
        existing_user = conn.execute(
            'SELECT id FROM users WHERE username = ? OR email = ?',
            (username, email)
        ).fetchone()
        
        if existing_user:
            flash('Пользователь с таким именем или email уже существует', 'error')
            conn.close()
            return render_template('auth/register.html')
        
        # Создаем пользователя
        password_hash = hash_password(password)
        conn.execute(
            'INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)',
            (username, email, password_hash)
        )
        conn.commit()
        conn.close()
        
        flash('Регистрация успешна! Теперь вы можете войти в систему', 'success')
        return redirect(url_for('login'))
    
    return render_template('auth/register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Вход в систему"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('Введите имя пользователя и пароль', 'error')
            return render_template('auth/login.html')
        
        conn = get_db_connection()
        user = conn.execute(
            'SELECT * FROM users WHERE username = ? OR email = ?',
            (username, username)
        ).fetchone()
        conn.close()
        
        if not user or user['password_hash'] != hash_password(password):
            flash('Неверное имя пользователя или пароль', 'error')
            return render_template('auth/login.html')
        
        if not user['is_active']:
            flash('Аккаунт заблокирован', 'error')
            return render_template('auth/login.html')
        
        # Создаем сессию
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        
        flash(f'Добро пожаловать, {user["username"]}!', 'success')
        return redirect(url_for('index'))
    
    return render_template('auth/login.html')

@app.route('/logout')
def logout():
    """Выход из системы"""
    session.clear()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('index'))

# ===== РОУТЫ КОММЕНТАРИЕВ =====

@app.route('/api/comments/<anime_id>')
def get_comments(anime_id):
    """Получение комментариев к аниме"""
    try:
        episode = request.args.get('episode', type=int)
        sort_by = request.args.get('sort', 'newest')  # newest, oldest, rating
        show_spoilers = request.args.get('spoilers', 'false') == 'true'
        
        conn = get_db_connection()
        
        # Базовый запрос
        query = '''
            SELECT c.*, u.username, u.avatar_url, u.role,
                   COUNT(CASE WHEN cv.vote_type = 'like' THEN 1 END) as likes,
                   COUNT(CASE WHEN cv.vote_type = 'dislike' THEN 1 END) as dislikes
            FROM comments c
            LEFT JOIN users u ON c.user_id = u.id
            LEFT JOIN comment_votes cv ON c.id = cv.comment_id
            WHERE c.anime_id = ?
        '''
        params = [anime_id]
        
        # Фильтр по эпизоду
        if episode:
            query += ' AND c.episode_number = ?'
            params.append(episode)
        
        # Фильтр спойлеров
        if not show_spoilers:
            query += ' AND c.is_spoiler = 0'
        
        query += ' GROUP BY c.id'
        
        # Сортировка
        if sort_by == 'oldest':
            query += ' ORDER BY c.created_at ASC'
        elif sort_by == 'rating':
            query += ' ORDER BY (c.likes - c.dislikes) DESC, c.created_at DESC'
        else:  # newest
            query += ' ORDER BY c.created_at DESC'
        
        comments = conn.execute(query, params).fetchall()
        conn.close()
        
        comments_list = []
        for comment in comments:
            comment_dict = dict(comment)
            comment_dict['created_at'] = datetime.fromisoformat(comment_dict['created_at']).strftime('%d.%m.%Y %H:%M')
            comments_list.append(comment_dict)
        
        return jsonify({
            'success': True,
            'comments': comments_list
        })
        
    except Exception as e:
        logger.error(f"Ошибка получения комментариев: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/comments', methods=['POST'])
@login_required
def add_comment():
    """Добавление комментария"""
    try:
        data = request.get_json()
        
        anime_id = data.get('anime_id')
        content = data.get('content', '').strip()
        is_spoiler = data.get('is_spoiler', False)
        rating = data.get('rating')
        episode_number = data.get('episode_number')
        
        if not anime_id or not content:
            return jsonify({'success': False, 'error': 'Заполните все поля'}), 400
        
        if len(content) < 10:
            return jsonify({'success': False, 'error': 'Комментарий слишком короткий (минимум 10 символов)'}), 400
        
        if rating and (rating < 1 or rating > 10):
            return jsonify({'success': False, 'error': 'Рейтинг должен быть от 1 до 10'}), 400
        
        conn = get_db_connection()
        
        # Проверяем, не оставлял ли пользователь уже комментарий к этому аниме/эпизоду
        existing = conn.execute(
            'SELECT id FROM comments WHERE user_id = ? AND anime_id = ? AND episode_number = ?',
            (session['user_id'], anime_id, episode_number)
        ).fetchone()
        
        if existing:
            conn.close()
            return jsonify({'success': False, 'error': 'Вы уже оставили комментарий к этому эпизоду'}), 400
        
        # Добавляем комментарий
        cursor = conn.execute(
            '''INSERT INTO comments (anime_id, user_id, content, is_spoiler, rating, episode_number)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (anime_id, session['user_id'], content, is_spoiler, rating, episode_number)
        )
        comment_id = cursor.lastrowid
        
        # Получаем добавленный комментарий с данными пользователя
        comment = conn.execute(
            '''SELECT c.*, u.username, u.avatar_url, u.role
               FROM comments c
               LEFT JOIN users u ON c.user_id = u.id
               WHERE c.id = ?''',
            (comment_id,)
        ).fetchone()
        
        conn.commit()
        conn.close()
        
        comment_dict = dict(comment)
        comment_dict['created_at'] = datetime.fromisoformat(comment_dict['created_at']).strftime('%d.%m.%Y %H:%M')
        comment_dict['likes'] = 0
        comment_dict['dislikes'] = 0
        
        return jsonify({
            'success': True,
            'comment': comment_dict
        })
        
    except Exception as e:
        logger.error(f"Ошибка добавления комментария: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/comments/<int:comment_id>/vote', methods=['POST'])
@login_required
def vote_comment(comment_id):
    """Лайк/дизлайк комментария"""
    try:
        data = request.get_json()
        vote_type = data.get('vote_type')  # 'like' или 'dislike'
        
        if vote_type not in ['like', 'dislike']:
            return jsonify({'success': False, 'error': 'Неверный тип голоса'}), 400
        
        conn = get_db_connection()
        
        # Проверяем существование комментария
        comment = conn.execute('SELECT id FROM comments WHERE id = ?', (comment_id,)).fetchone()
        if not comment:
            conn.close()
            return jsonify({'success': False, 'error': 'Комментарий не найден'}), 404
        
        # Проверяем, голосовал ли пользователь уже
        existing_vote = conn.execute(
            'SELECT vote_type FROM comment_votes WHERE comment_id = ? AND user_id = ?',
            (comment_id, session['user_id'])
        ).fetchone()
        
        if existing_vote:
            if existing_vote['vote_type'] == vote_type:
                # Убираем голос
                conn.execute(
                    'DELETE FROM comment_votes WHERE comment_id = ? AND user_id = ?',
                    (comment_id, session['user_id'])
                )
            else:
                # Меняем голос
                conn.execute(
                    'UPDATE comment_votes SET vote_type = ? WHERE comment_id = ? AND user_id = ?',
                    (vote_type, comment_id, session['user_id'])
                )
        else:
            # Добавляем новый голос
            conn.execute(
                'INSERT INTO comment_votes (comment_id, user_id, vote_type) VALUES (?, ?, ?)',
                (comment_id, session['user_id'], vote_type)
            )
        
        # Получаем обновленные лайки/дизлайки
        votes = conn.execute(
            '''SELECT 
                   COUNT(CASE WHEN vote_type = 'like' THEN 1 END) as likes,
                   COUNT(CASE WHEN vote_type = 'dislike' THEN 1 END) as dislikes
               FROM comment_votes WHERE comment_id = ?''',
            (comment_id,)
        ).fetchone()
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'likes': votes['likes'],
            'dislikes': votes['dislikes']
        })
        
    except Exception as e:
        logger.error(f"Ошибка голосования: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
@login_required
def delete_comment(comment_id):
    """Удаление комментария"""
    try:
        conn = get_db_connection()
        
        # Проверяем права на удаление
        comment = conn.execute(
            'SELECT user_id FROM comments WHERE id = ?', (comment_id,)
        ).fetchone()
        
        if not comment:
            conn.close()
            return jsonify({'success': False, 'error': 'Комментарий не найден'}), 404
        
        # Только автор или админ может удалить
        if comment['user_id'] != session['user_id'] and session.get('role') != 'admin':
            conn.close()
            return jsonify({'success': False, 'error': 'Недостаточно прав'}), 403
        
        # Удаляем комментарий и связанные голоса
        conn.execute('DELETE FROM comment_votes WHERE comment_id = ?', (comment_id,))
        conn.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"Ошибка удаления комментария: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== НОВЫЕ API ДЛЯ АЛЬТЕРНАТИВНЫХ ПОСТЕРОВ =====

@app.route('/api/anime/<anime_id>/alternative-poster')
def get_alternative_poster(anime_id):
    """Получение альтернативного постера от Kodik для аниме"""
    try:
        # Извлекаем shikimori_id если anime_id имеет префикс
        shikimori_id = None
        if anime_id.startswith('shiki_'):
            shikimori_id = anime_id.replace('shiki_', '')
        
        # Ищем в Kodik
        kodik_results = None
        if shikimori_id:
            kodik_results = anime_service.kodik.search_by_shikimori_id(shikimori_id)
        
        # Если не нашли по ID, пробуем поиск по названию
        if not kodik_results or not kodik_results.get('results'):
            # Получаем информацию об аниме из Shikimori для поиска по названию
            if shikimori_id:
                shikimori_anime = anime_service.shikimori.get_anime(shikimori_id)
                if shikimori_anime:
                    title = shikimori_anime.get('russian') or shikimori_anime.get('name', '')
                    if title:
                        kodik_results = anime_service.kodik.search_by_title(title)
        
        # Проверяем результаты
        if kodik_results and kodik_results.get('results'):
            kodik_anime = kodik_results['results'][0]
            kodik_material = kodik_anime.get('material_data', {})
            kodik_poster = kodik_material.get('poster_url')
            
            if kodik_poster:
                # Проверяем доступность постера от Kodik
                if anime_service._check_image_availability(kodik_poster):
                    return jsonify({
                        'success': True,
                        'poster_url': kodik_poster,
                        'source': 'kodik'
                    })
        
        # Если не нашли постер от Kodik, возвращаем placeholder
        return jsonify({
            'success': True,
            'poster_url': 'https://via.placeholder.com/300x400/8B5CF6/FFFFFF?text=Нет+постера',
            'source': 'placeholder'
        })
        
    except Exception as e:
        logger.error(f"Ошибка при получении альтернативного постера для {anime_id}: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/posters/stats')
def poster_stats():
    """Статистика по постерам (для отладки)"""
    try:
        # Получаем небольшую выборку аниме для анализа
        sample_anime = anime_service.get_popular_anime(20)
        
        stats = {
            'total_checked': 0,
            'shikimori_ok': 0,
            'shikimori_failed': 0,
            'kodik_available': 0,
            'placeholder_used': 0,
            'problems_detected': []
        }
        
        for anime in sample_anime:
            stats['total_checked'] += 1
            poster_url = anime.get('material_data', {}).get('poster_url', '')
            
            if poster_url.startswith('https://via.placeholder.com'):
                stats['placeholder_used'] += 1
            elif '404' in poster_url.lower() or any(error in poster_url.lower() for error in ['not_found', 'notfound', 'error', 'missing']):
                stats['shikimori_failed'] += 1
                stats['problems_detected'].append({
                    'anime': anime.get('title', 'Unknown'),
                    'url': poster_url,
                    'issue': '404 or error indicator in URL'
                })
            elif poster_url and anime_service._check_image_availability(poster_url):
                stats['shikimori_ok'] += 1
            else:
                stats['shikimori_failed'] += 1
                stats['problems_detected'].append({
                    'anime': anime.get('title', 'Unknown'),
                    'url': poster_url,
                    'issue': 'HTTP check failed'
                })
                
                # Проверяем наличие альтернативы от Kodik
                if anime.get('kodik_id'):
                    stats['kodik_available'] += 1
        
        return jsonify({
            'success': True,
            'stats': stats
        })
        
    except Exception as e:
        logger.error(f"Ошибка при получении статистики постеров: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== ОСНОВНЫЕ РОУТЫ =====

@app.route('/')
def index():
    """Главная страница"""
    try:
        logger.info("Загрузка главной страницы")
        
        # Получаем информацию о текущем сезоне
        current_season, current_year = get_current_season()
        season_name_ru = get_season_name_ru(current_season)
        season_emoji = get_season_emoji(current_season)
        
        logger.info(f"Текущий сезон: {current_season} {current_year} ({season_name_ru})")
        
        # Получаем сезонные аниме (новинки текущего сезона)
        seasonal_anime = anime_service.get_seasonal_anime(current_season, current_year, 12)
        
        # Получаем популярные аниме всех времён  
        popular_anime = anime_service.get_popular_anime(24)
        
        # Если сезонных аниме мало или нет, дополняем популярными
        if len(seasonal_anime) < 6:
            logger.info("Сезонных аниме мало, дополняем популярными")
            seasonal_anime.extend(popular_anime[:6])
            seasonal_anime = seasonal_anime[:6]  # Ограничиваем до 6
        
        # Получаем текущего пользователя
        current_user = get_current_user()
        
        return render_template('index.html', 
                             seasonal_anime=seasonal_anime,
                             popular_anime=popular_anime,
                             current_season=current_season,
                             current_year=current_year,
                             season_name_ru=season_name_ru,
                             season_emoji=season_emoji,
                             current_user=current_user)
    except Exception as e:
        logger.error(f"Ошибка на главной странице: {e}")
        return render_template('index.html', 
                             seasonal_anime=[], 
                             popular_anime=[],
                             current_season='summer',
                             current_year=2025,
                             season_name_ru='летнего',
                             season_emoji='☀️',
                             error=str(e),
                             current_user=get_current_user())

@app.route('/catalog')
def catalog():
    """Каталог аниме"""
    try:
        query = request.args.get('q', '').strip()
        genre = request.args.get('genre', '')
        year_from = request.args.get('year_from', '')
        year_to = request.args.get('year_to', '')
        status = request.args.get('status', '')
        anime_type = request.args.get('type', '')
        season = request.args.get('season', '')  # Новый параметр сезона
        year = request.args.get('year', '')      # Год для сезона
        
        logger.info(f"Каталог запрос: q='{query}', genre={genre}, year_from={year_from}, year_to={year_to}, status={status}, type={anime_type}, season={season}, year={year}")
        
        filters = {}
        if genre:
            filters['genre'] = genre
        if year_from:
            filters['year_from'] = year_from
        if year_to:
            filters['year_to'] = year_to
        if status:
            filters['status'] = status
        if anime_type:
            filters['type'] = anime_type
        if season and year:
            filters['season'] = f"{season}_{year}"
            
        anime_list = []
        
        # Если запрос сезонного аниме
        if season and year:
            anime_list = anime_service.get_seasonal_anime(season, int(year), 24)
        elif query or filters:
            anime_list = anime_service.search_anime(query, filters)
        else:
            # По умолчанию показываем популярные
            anime_list = anime_service.get_popular_anime(24)
            
        logger.info(f"Возвращаем {len(anime_list)} аниме")
        return render_template('catalog.html', 
                             anime_list=anime_list,
                             query=query,
                             filters=request.args,
                             current_user=get_current_user())
    except Exception as e:
        logger.error(f"Ошибка в каталоге: {e}")
        return render_template('catalog.html', anime_list=[], 
                             error=str(e), query='', filters={},
                             current_user=get_current_user())

@app.route('/watch/<anime_id>')
def watch(anime_id):
    """Страница просмотра аниме"""
    try:
        shikimori_id = request.args.get('sid')
        logger.info(f"Просмотр аниме: id={anime_id}, shikimori_id={shikimori_id}")
        
        anime = anime_service.get_anime_details(anime_id, shikimori_id)
        
        if not anime:
            logger.warning(f"Аниме не найдено: {anime_id}")
            return render_template('error.html', 
                                 message="Аниме не найдено"), 404
        
        # Сохраняем в историю просмотров если пользователь авторизован
        current_user = get_current_user()
        if current_user:
            try:
                conn = get_db_connection()
                conn.execute(
                    '''INSERT OR REPLACE INTO watch_history 
                       (user_id, anime_id, episode_number, season_number, last_watched)
                       VALUES (?, ?, 1, 1, CURRENT_TIMESTAMP)''',
                    (current_user['id'], anime_id)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Ошибка сохранения истории: {e}")
            
        return render_template('watch.html', anime=anime, current_user=current_user)
    except Exception as e:
        logger.error(f"Ошибка на странице просмотра: {e}")
        return render_template('error.html', 
                             message=f"Ошибка: {e}"), 500

@app.route('/subscription')
def subscription():
    """Страница подписки"""
    return render_template('subscription.html', current_user=get_current_user())

@app.route('/api/search')
def api_search():
    """API для поиска аниме"""
    try:
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({"error": "Запрос не может быть пустым"}), 400
            
        results = anime_service.search_anime(query)
        return jsonify({"results": results})
    except Exception as e:
        logger.error(f"Ошибка API поиска: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health')
def health_check():
    """Проверка состояния сервиса"""
    try:
        # Проверяем доступность API
        shikimori_status = "OK"
        kodik_status = "OK" if anime_service.kodik.token else "NO_TOKEN"
        
        # Тестовый запрос к Shikimori
        try:
            test_result = anime_service.shikimori._make_request('animes', {'limit': 1})
            if not test_result:
                shikimori_status = "FAILED"
        except:
            shikimori_status = "FAILED"
        
        return jsonify({
            "status": "OK",
            "shikimori_api": shikimori_status,
            "kodik_api": kodik_status,
            "current_season": get_current_season(),
            "timestamp": datetime.now().isoformat(),
            "poster_cache_size": len(anime_service.poster_cache)
        })
    except Exception as e:
        return jsonify({
            "status": "ERROR",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/api/test-posters')
def test_posters():
    """Тестирование постеров для диагностики"""
    try:
        # Получаем одно аниме для тестирования
        test_anime = anime_service.shikimori._make_request('animes', {'limit': 1})
        
        if test_anime and isinstance(test_anime, list) and test_anime:
            anime = test_anime[0]
            poster_url = anime_service._get_poster_url(anime)
            
            return jsonify({
                "anime_id": anime.get('id'),
                "anime_name": anime.get('name'),
                "raw_image_data": anime.get('image', {}),
                "processed_poster_url": poster_url,
                "shikimori_full_response": anime,
                "poster_availability": anime_service._check_image_availability(poster_url) if poster_url else False
            })
        else:
            return jsonify({"error": "Не удалось получить тестовые данные"})
            
    except Exception as e:
        return jsonify({"error": str(e)})

        # ===== ЮРИДИЧЕСКИЕ СТРАНИЦЫ =====

@app.route('/terms-of-service')
def terms_of_service():
    """Пользовательское соглашение"""
    return render_template('legal/terms.html', current_user=get_current_user())

@app.route('/privacy-policy') 
def privacy_policy():
    """Политика конфиденциальности"""
    return render_template('legal/privacy.html', current_user=get_current_user())

@app.route('/cookie-policy')
def cookie_policy():
    """Политика использования cookies"""
    return render_template('legal/cookies.html', current_user=get_current_user())

@app.route('/dmca')
def dmca():
    """DMCA процедуры"""
    return render_template('legal/dmca.html', current_user=get_current_user())

if __name__ == '__main__':
    # Инициализация при запуске
    current_season, current_year = get_current_season()
    season_name_ru = get_season_name_ru(current_season)
    season_emoji = get_season_emoji(current_season)
    
    logger.info(f"🚀 Запуск Anivest с интеграцией Shikimori + Kodik")
    logger.info(f"🌟 Текущий сезон: {season_emoji} {season_name_ru} {current_year}")
    logger.info(f"🖼️ Включена улучшенная система проверки постеров")
    
    # Проверяем доступность API
    if anime_service.kodik.get_token():
        logger.info("✅ Kodik API готов")
    else:
        logger.warning("⚠️ Kodik API недоступен - будут показаны только данные Shikimori")
    
    app.run(debug=True, host='0.0.0.0', port=5000)