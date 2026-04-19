# Raspmath
Бот в тг для просмотра расписания маего любимава вуза ИМИТ
# 🎓 RaspMath — Telegram-бот расписания ИМИТ ИГУ

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://python.org)
[![Aiogram](https://img.shields.io/badge/Aiogram-3.13-blue?logo=telegram)](https://docs.aiogram.dev/)
[![MySQL](https://img.shields.io/badge/MySQL-8.0-orange?logo=mysql)](https://mysql.com)
[![License](https://img.shields.io/badge/License-MIT-green)](./LICENSE)

Удобный асинхронный Telegram-бот для просмотра расписания занятий **Института математики и информационных технологий ИГУ** (сайт [raspmath.isu.ru](https://raspmath.isu.ru/schedule/)).

> 🔥 Быстрое получение пар на сегодня, завтра, текущую и следующую неделю.  
> 💾 Кэширование в MySQL, умный парсинг JSON API, админ‑рассылки и статистика.

---

## ✨ Возможности

- 📋 **Пошаговый выбор группы** — бакалавриат / магистратура → курс → конкретная группа.
- 📅 **Расписание на день / неделю** — с учётом чётности недели, типа занятия (лекция, практика, лаба), аудитории и преподавателя.
- 🔁 **Смена группы** в один клик через главное меню.
- 🐞 **Отправка баг‑репортов** администратору (текст + фото).
- 👑 **Административные команды** (только для указанного `ADMIN_USER_ID`):
  - `/broadcast` — рассылка сообщения всем пользователям бота.
  - `/stats` — количество пользователей и выгрузка CSV.
- ⚡ **Кэширование расписания** — запрос к сайту один раз в 7 дней, далее данные берутся из MySQL.
- 🌐 **Часовой пояс Иркутска** (Asia/Irkutsk).

---

## 🚀 Быстрый старт

### 1. Клонируй репозиторий
bash
git clone https://github.com/sswwaaggeerr/Raspmath.git
cd Raspmath

## 2. Создай виртуальное окружение и установи зависимости
bash
python -m venv venv
source venv/bin/activate      # Linux/macOS
venv\Scripts\activate         # Windows
pip install -r requirements.txt

## 3. Настрой MySQL
Создай базу данных и пользователя:

sql
CREATE DATABASE isu_bot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'isu_bot'@'localhost' IDENTIFIED BY 'твой_пароль';
GRANT ALL PRIVILEGES ON isu_bot.* TO 'isu_bot'@'localhost';
FLUSH PRIVILEGES;

## 4. Создай файл .env
Скопируй содержимое из .env.example (или создай вручную) и заполни своими данными:

ini
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
ADMIN_USER_ID=123456789

DB_HOST=localhost
DB_PORT=3306
DB_USER=isu_bot
DB_PASSWORD=твой_пароль
DB_NAME=isu_bot

## 5. Запусти бота
bash
python main.py
После первого запуска бот автоматически создаст все необходимые таблицы в базе данных.

## 📦 Структура проекта
Файл	Назначение
main.py	Точка входа, инициализация бота, обработчики сообщений, FSM
database.py	Работа с MySQL (пул соединений), хранилища UserSettingsStore, MySQLStorage, ScheduleCacheStore
schedule_raspmath.py	Парсинг страницы /schedule/ и запросов к /fillSchedule
requirements.txt	Зависимости Python
.env.example	Пример конфигурационного файла
.gitignore	Исключение секретных и временных файлов из Git

## 🛠 Используемые технологии
Aiogram 3.x — асинхронный фреймворк для Telegram Bot API.

aiohttp — HTTP‑клиент для запросов к raspmath.isu.ru.

BeautifulSoup4 — парсинг HTML‑страницы с выбором групп.

aiomysql — асинхронный драйвер для MySQL.

python‑dotenv — загрузка переменных окружения из .env.

zoneinfo — работа с часовым поясом Иркутска.

## 🤝 Вклад в проект
Буду рад пул‑реквестам!
Если нашли баг или есть идея по улучшению — создавайте Issue.

## 📄 Лицензия
MIT © sswwaaggeerr

## Сделано с ❤️ для студентов ИМИТ ИГУ.
