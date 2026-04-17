# Варианты ускорения загрузки screener/funding/arb

## Корневая проблема
4 uvicorn workers = 4 отдельных Python-процесса. `_cache` — in-process dict.
Только Worker #1 (broadcaster) держит кэш горячим. Workers #2-4 имеют пустой
кэш → каждый REST-запрос рефетчит 12 бирж = 20-30s.

## Требования
- Цены обновляются каждые 3-5с
- Стакан обновляется каждые 150ms (прямой fetch к бирже — уже быстро)
- Ничего не может быть медленнее или реже текущего

---

## Вариант A: nginx proxy_cache (⭐ РЕКОМЕНДУЮ)

**Суть**: nginx кэширует ответы `/api/screener/funding` и `/api/screener/arbitrage`
на 5-10 секунд. Любой worker отдаёт — nginx запоминает и следующие запросы
отдаёт из своей памяти за ~1ms.

| Метрика | До | После |
|---|---|---|
| /funding cold | 20-30s | **≤5s** (первый запрос к Worker #1) |
| /funding warm | 3.5s | **~1ms** (nginx cache hit) |
| /arbitrage cold | 18s | **≤1s** (10s result cache + nginx) |
| /pair | 10ms | 10ms (без изменений) |
| Orderbook | 250-385ms | без изменений |
| WS live updates | 10s | без изменений |

**Плюсы**: 0 строк Python. Только nginx.conf. Работает с любым числом workers.
**Минусы**: Auth не проверяется на cached-ответах (можно решить через proxy_cache_key с токеном).
**Сложность**: S (10 строк nginx конфига)

## Вариант B: Redis shared cache

**Суть**: Broadcaster пишет JSON в Redis. Все workers читают из Redis (~1ms).

| Метрика | До | После |
|---|---|---|
| /funding | 20-30s → 3.5s | **~5ms** (Redis GET) |
| /arbitrage | 18s → 0.27s | **~5ms** (Redis GET) |

**Плюсы**: Чистый, масштабируемый, auth сохраняется.
**Минусы**: +Redis в docker-compose (+50MB RAM). Изменения в arbitrage_service + get_funding_data.
**Сложность**: M (Redis setup + 30 строк Python)

## Вариант C: Файловый кэш (mmap)

**Суть**: Broadcaster пишет JSON в файл (/tmp/funding.json). Workers читают файл.

| Метрика | До | После |
|---|---|---|
| /funding | 20-30s | **~10ms** (file read) |

**Плюсы**: Нет зависимостей. Работает в Docker (shared /tmp через volume).
**Минусы**: Docker workers = отдельные контейнеры (у нас 1 контейнер с 4 workers → /tmp shared ✓). Race condition при записи/чтении. Нужен atomic write.
**Сложность**: M (20 строк Python + volume mount)

## Вариант D: 1 worker + orjson + optimized fetchers

**Суть**: Вернуться к 1 worker, но поставить orjson (10× быстрее JSON parse),
вынести ВСЕ I/O в thread pool, агрессивно кэшировать.

| Метрика | До | После |
|---|---|---|
| /funding | 3.5s (warm) | **~0.5s** (orjson + less GIL contention) |

**Плюсы**: Простейшая архитектура (1 процесс).
**Минусы**: GIL всё равно блокирует при большом I/O; на пиках снова 3-5s.
**Сложность**: S-M

---

## Рекомендация: A + C (nginx cache + file fallback)

1. **nginx proxy_cache** для `/api/screener/funding` и `/api/screener/arbitrage`
   — 5s TTL. Клиент получает мгновенный ответ. Auth: включить `proxy_cache_key`
   с проверкой что запрос авторизован (или не кэшировать 401).

2. **Broadcaster пишет /tmp/funding_cache.json** каждые 10s — workers читают
   файл при cache miss вместо рефетча 12 бирж. Fallback на live fetch если
   файл stale.

Итого: первый запрос ~1ms (nginx), fallback ~10ms (file), worst case ~3.5s
(live fetch на Worker #1). WS обновляет каждые 10s. Orderbook 150ms poll
не трогаем.
