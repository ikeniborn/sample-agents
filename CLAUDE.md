# Ограничения

Целевой каталог агента pac1-py

# Разработка
Использовать паттерн хардкода при доработке агента. 

# Тестирование

Пример запуска агента
```bash
TZ=Europe/Moscow ts=$(TZ=Europe/Moscow date +"%Y%m%d_%H%M%S") && logfile="/home/ikeniborn/Documents/Project/sample-agents/tmp/${ts}_qwen3.5-9b.log" && echo "Лог: $logfile" && TASK_TIMEOUT_S=900 uv run python main.py t01 2>&1 | tee "$logfile"
```
