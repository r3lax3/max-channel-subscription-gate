# MAX Bot API: исследование для порта subscription gate

Дата: 2026-10-16. Задача: перенести Telegram-бота subscription gate (aiogram 3) в MAX. Нужно сохранить ту же логику: оплата через Prodamus, доступ в закрытую группу или канал, удаление участника по окончании подписки, напоминания и админ-меню.

Пометки:
- **[LIVE]** означает, что я проверил это запросом к API с токеном бота из `.env`. Выполнялись только GET-запросы. Ничего не изменялось, сообщения не отправлялись.
- **[DOCS]** означает официальную документацию dev.max.ru.
- **[SCHEMA]** означает официальную OpenAPI-спецификацию [`max-messenger/api-schema/schema.yaml`](https://github.com/max-messenger/api-schema/blob/main/schema.yaml) (v0.0.33, коммит от 2026-09-18).
- **[SKILL]** означает официальный [MAX Messenger Agent Skill](https://github.com/max-messenger/max-agents-skill). Его упоминает changelog MAX за октябрь 2026. Это вторичная выжимка от того же вендора, и ключевые утверждения из неё я сверял с DOCS.

---

## 0. Главное

1. **Бот не может удалять подписчиков канала, только участников групповых чатов.** Это закрывает вариант «закрытый канал». Делать нужно **закрытый групповой чат**. Подробности в §4.
2. **Персональных или одноразовых инвайт-ссылок в API нет.** Есть только общая ссылка чата (`Chat.link`), её создаёт и меняет человек в клиенте. Контроль входа строится так: событие `user_added`, затем проверка оплаты в БД, затем kick. См. §5.
3. **Аналога `chat_join_request` нет.**
4. **`GET /chats` удалён с июня 2026** (живой запрос возвращает 404). Список чатов бот должен собирать сам из событий `bot_added` и `bot_removed`.
5. **`POST /chats/{chatId}/members` (добавление участников) ограничен с 9 сентября 2026**, и в канал им добавлять нельзя. Значит, добавить оплатившего пользователя в чат API-вызовом нельзя, вход возможен только по ссылке.
6. **Новый хост `platform-api2.max.ru` использует сертификат, выпущенный Минцифры.** Без Russian Trusted Root CA в trust store TLS не проходит (проверено вживую).
7. **Long polling документация называет «только для разработки и тестирования».** Для production MAX рекомендует webhook: HTTPS, порт 443, ответ 200 не позже чем через 30 секунд.
8. **Платежей внутри MAX Bot API нет.** Оплата остаётся внешней, через Prodamus по ссылке.

---

## 1. Базовый URL, авторизация, лимиты

### Базовый URL
- `https://platform-api2.max.ru`. [DOCS, changelog](https://dev.max.ru/docs-api/changelog-api): «С 19 июля 2026 для корректной работы чат-ботов и мини-приложений необходимо направлять запросы на домен platform-api2.max.ru вместо platform-api.max.ru, а также добавить сертификат Минцифры в список доверенных».
- [SCHEMA] `info.description`: «Methods are called by sending HTTPS requests to platform-api2.max.ru domain».
- **[LIVE]** Старый `platform-api.max.ru` всё ещё отвечает: `GET /me` вернул 200. Дата его отключения не объявлена, поэтому опираться на него нельзя.
- **[LIVE]** `curl https://platform-api2.max.ru/me` с системным trust store падает с ошибкой `exit 60` (SSL). Цепочка такая: `CN=*.max.ru, O=MAX LLC` → `Russian Trusted Sub CA` → `Russian Trusted Root CA` (Минцифры). С `--cacert`, в который положены Root и Sub CA с `https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer` и `.../russian_trusted_sub_ca.cer`, запрос возвращает 200. Root действует до 2032-02-27, Sub CA до 2027-03-06 (вывод `openssl x509`).
  - В Python добавлять сертификат надо так: `ssl.create_default_context()` + `load_verify_locations(cafile=...)`. Глобальный trust store лучше не трогать. Библиотека `maxapi` уже содержит `russiantrustedca.pem` и делает именно это (`maxapi/client/ssl.py`).

### Авторизация
- Только HTTP-заголовок `Authorization: <token>`, **без** `Bearer`. [DOCS, /docs-api](https://dev.max.ru/docs-api): «Передача токена через query-параметры больше не поддерживается — используйте заголовок Authorization: <access_token>».
- **[LIVE]** `GET /me?access_token=...` → `401 {"code":"verify.token","message":"Query parameter access_token is deprecated, use Authorization header"}`. Запрос без токена → `401 {"code":"verify.token","message":"No access token"}`.

### Лимиты
- [DOCS, «Подготовка и настройка»](https://dev.max.ru/docs/chatbots/bots-coding/prepare): «Для стабильной работы ботов убедитесь, что максимальное количество запросов в секунду на platform-api2.max.ru — 30 rps». Не указано, считается это на токен или глобально. Закладываться стоит на ≤30 rps на бота.
- [DOCS, POST /answers](https://dev.max.ru/docs-api/methods/POST/answers): «Можно отправлять не более двух ответов в секунду в один диалог, групповой чат или канал».
- [SCHEMA]: «Minimum polling time - 300ms».
- При превышении лимита приходит HTTP `429` [SCHEMA]. **[LIVE]** В ответах нет заголовков `X-RateLimit-*` или `Retry-After` (проверено на `GET /me`).
- Для рассылки напоминаний нужен свой rate limiter, например token bucket на ~25 rps.

### Формат данных
- Время в **миллисекундах** Unix (int64), UTC [SCHEMA]: «Date/time fields are represented as the number of milliseconds…».
- `user_id` и `chat_id` имеют тип int64. ID групп и каналов **отрицательные**: в схеме `x-pattern: \-?\d+`.

---

## 2. Получение обновлений

### Long polling: `GET /updates`
[DOCS](https://dev.max.ru/docs-api/methods/GET/updates). Query-параметры:
| параметр | тип | описание |
|---|---|---|
| `limit` | int 1–1000, по умолчанию 100 | максимальное число событий |
| `timeout` | int 0–90 с, по умолчанию 30 | таймаут долгого опроса |
| `marker` | int64, nullable | Если не передан или `null`, вернётся только последнее событие. Если передан `marker` из прошлого ответа, вернутся все события после него. «Все предыдущие считаются прочитанными» |
| `types` | csv | фильтр, например `message_created,message_callback` |

Ответ: `{"updates":[Update...], "marker": <int64>}`. **[LIVE]** `GET /updates?timeout=0&limit=1` → `{"updates":[],"marker":83010}`.

Оговорки [DOCS]: «Получение обновлений с помощью Long Polling ограничено по скорости и сроку хранения событий — этот способ не подходит для production-окружения. Рекомендуем на всех этапах работы использовать Webhook». Конкретный срок хранения событий в документации не указан. Событие `bot_admin_permissions_changed` через long polling пока не приходит.

### Webhook: `POST /subscriptions`
[DOCS](https://dev.max.ru/docs-api/methods/POST/subscriptions)
```json
POST /subscriptions
{
  "url": "https://bot.example.com/max/webhook",
  "update_types": ["message_created", "message_callback", "bot_started", "bot_added", "bot_removed", "user_added", "user_removed"],
  "secret": "abc_DEF-123"
}
```
- `secret` содержит 5–256 символов `[A-Za-z0-9_-]`. MAX возвращает его в заголовке **`X-Max-Bot-Api-Secret`** каждого запроса. Сравнивать нужно через `hmac.compare_digest`.
- Требования: HTTPS только на **порту 443** (порт в URL не указывается). Сертификат от доверенного CA или от Минцифры, самоподписанные не принимаются. Домен должен совпадать с CN/SAN, сервер должен отдавать полную цепочку. HTTP и self-signed не принимаются с 25 мая.
- Endpoint должен ответить **200 не позже чем через 30 секунд**. Иначе MAX повторит доставку до 10 раз с нарастающим интервалом, начиная с 60 секунд. Если за **8 часов** ни одна доставка не прошла, бот **автоматически отписывается**. Поэтому нужен мониторинг `GET /subscriptions`.
- Возможны повторные доставки одного события, поэтому нужна идемпотентность.
- `GET /subscriptions` возвращает список подписок, `DELETE /subscriptions?url=...` удаляет подписку. **[LIVE]** `GET /subscriptions` → `{"subscriptions":[]}`, то есть сейчас бот на webhook не подписан.
- Webhook и polling взаимоисключающие. В README maxapi: «Если у бота есть активные Webhook-подписки, события не будут приходить через start_polling».
- [DOCS, admins](https://dev.max.ru/docs-api/methods/POST/chats/-chatId-/members/admins): право `read_all_messages` «важно при назначении ботов: без него тот не будет получать обновления через Webhook». Боту в группе обязательно выдать `read_all_messages`.

### Типы `update_type`
[DOCS, объект Update](https://dev.max.ru/docs-api/objects/Update), цитаты:
- `bot_added`: «бот добавлен в чат или канал». Поля `chat_id`, `user`, `is_channel`.
- `bot_removed`: «бот удалён из чата или канала».
- `bot_started`: «пользователь впервые начал общение с ботом или возобновил после остановки». Поля `chat_id`, `user`, `payload` (deep link), `user_locale`.
- `bot_stopped`: «пользователь остановил или удалил бота». Аналог `my_chat_member` / kicked в Telegram. После этого события писать пользователю бесполезно.
- `message_created`, `message_edited`, `message_removed`.
- `message_callback`: «пользователь нажал на кнопку». Поля `callback{callback_id, payload, user, timestamp}`, `message`.
- `user_added`: «в чат или канал добавлен или **перешёл по ссылке** новый пользователь». Поля `chat_id`, `user`, `is_channel`, `inviter_id` (nullable).
- `user_removed`: «пользователь удалён или покинул чат или канал». Поля `chat_id`, `user`, `is_channel`, `admin_id` (опционально).
- `chat_title_changed`, `dialog_cleared`, `dialog_muted`, `dialog_unmuted`, `dialog_removed`.
- `comment_created`, `comment_edited`, `comment_removed` (комментарии к постам канала, добавлены в августе 2026).
- `bot_admin_permissions_changed` (октябрь 2026, только webhook).

Пример [SCHEMA]:
```json
{"update_type":"bot_started","timestamp":1573226679188,"chat_id":1234567890,
 "user":{"user_id":1234567890,"name":"Boris","username":"borisd84"},
 "payload":"any data meaningful to bot"}
```

**Аналога `chat_join_request` нет.** В схеме и в списке Update нет события заявки на вступление и нет методов approve/decline (поиск по `join|request|invite` в `schema.yaml` ничего не нашёл). Пользователь входит по ссылке сразу, бот узнаёт об этом только постфактум через `user_added`.

---

## 3. Сообщения

### Отправка: `POST /messages`
Получатель задаётся **query**-параметром `user_id` (личка) или `chat_id` (группа или канал). Ещё есть параметр `disable_link_preview` (bool). В тело получатель не передаётся [SCHEMA].
```http
POST /messages?user_id=514344027
Authorization: <token>
Content-Type: application/json

{
  "text": "Подписка активна до **01.11.2026**",
  "format": "markdown",
  "notify": true,
  "attachments": [
    {"type": "inline_keyboard", "payload": {"buttons": [
      [{"type": "link", "text": "Оплатить", "url": "https://example.payform.ru/?..."}],
      [{"type": "callback", "text": "Проверить статус", "payload": "status"}]
    ]}}
  ]
}
```
Ответ: `{"message": Message}`. `Message.body.mid` (string) нужен для редактирования и удаления.

`NewMessageBody` [SCHEMA]: `text` (≤4000 символов), `attachments`, `link` (`{"type":"reply"|"forward","mid":...}`), `notify` (по умолчанию true), `format` (`markdown` | `html`).

### Кнопки
[SCHEMA] Типы: `callback` (`payload` ≤1024), `link` (`url` ≤2048), `message`, `request_contact`, `request_geo_location`, `open_app`, `clipboard`. Текст кнопки 1–128 символов. Лимиты из [SKILL], со ссылкой на DOCS: ≤210 кнопок, ≤30 рядов, ≤7 кнопок в ряду, а для `link`, `open_app` и `request_*` ≤3 в ряду.

### Ответ на callback: `POST /answers?callback_id=...`
[DOCS](https://dev.max.ru/docs-api/methods/POST/answers): «Ответом может быть обновленное сообщение и/или одноразовое уведомление».
```json
POST /answers?callback_id=<callback.callback_id>
{"notification": "Готово", "message": {"text": "Новый текст", "attachments": [...]}}
```
`message` заменяет исходное сообщение с кнопкой. Это аналог `callback.message.edit_text` и `callback.answer` в одном вызове. Ограничение: ≤2 ответа в секунду на чат.

### Редактирование и удаление
- `PUT /messages?message_id=<mid>`, тело `NewMessageBody`. `attachments: null` оставляет вложения как есть, `[]` удаляет их [SKILL].
- `DELETE /messages?message_id=<mid>`.
- `GET /messages?chat_id=...&message_ids=...`, `GET /messages/{messageId}`. **[LIVE]** `GET /messages` без параметров → `400 proto.payload "ChatId & messageIds not found in request"`.

### Форматирование
[SCHEMA, «Text formatting»] `format: "markdown"`:
- `*курсив*` или `_курсив_`
- `**жирный**` или `__жирный__`
- `~~зачёркнутый~~`
- `++подчёркнутый++`
- `` `code` ``
- `^^выделение^^`
- `> цитата`
- `[ссылка](url)`
- `[имя](max://user/<user_id>)`: упоминание без username
- `# заголовок`

`format: "html"`: `<b> <strong> <i> <em> <del> <s> <ins> <u> <a href> <pre> <code> <mark> <blockquote> <h1>`, все остальные теги вырезаются. Это близко к HTML-режиму aiogram, но `<tg-spoiler>` и подобных тегов нет.

### Вложения
Загрузка идёт в три шага: `POST /uploads?type=image|video|audio|file` → загрузка файла на выданный `url` → `{"type":"image","payload":{"token":...}}` в `attachments`. Картинку можно передать сразу внешним URL: `{"type":"image","payload":{"url":"https://..."}}` [SCHEMA]. Если медиа ещё не обработано, приходит ошибка `attachment.not.ready`, и запрос нужно повторить [SKILL]. Для subscription gate вложения почти не нужны.

### Команды бота
`PATCH /me/commands` (июль 2026) задаёт меню команд, ≤32 команды [SKILL]. Это аналог `set_my_commands`.

---

## 4. Чаты, группы и каналы, участники

| метод | что делает | важное |
|---|---|---|
| `GET /chats` | **удалён** | **[LIVE]** `404 method.not.found "Path /chats is not recognized"` на обоих хостах |
| `GET /chats/{chatId}` | объект Chat | поля `type` (`chat`/`channel`/`dialog`), `status`, `title`, `participants_count`, `owner_id`, `is_public`, **`link`**, `description` |
| `GET /chats/{chatId}/members?user_ids=...&marker=&count=1..100` | участники, пагинация по `marker` | По `user_ids` можно проверить, состоит ли конкретный пользователь в чате |
| `POST /chats/{chatId}/members` `{"user_ids":[...]}` | добавить участников | **ограничен с 09.09.2026**; [DOCS](https://dev.max.ru/docs-api/methods/POST/chats/-chatId-/members): «Добавить подписчиков в канал с помощью этого метода нельзя» |
| `DELETE /chats/{chatId}/members?user_id=<id>&block=<bool>` | удалить участника | см. ниже |
| `GET /chats/{chatId}/members/me` | членство и права бота | |
| `GET /chats/{chatId}/members/admins` | список админов | бот должен быть админом |
| `POST /chats/{chatId}/members/admins` | назначить админа | семантика PUT, права перезаписываются полностью |
| `DELETE /chats/{chatId}/members/admins/{userId}` | снять админа | |
| `DELETE /chats/{chatId}/members/me` | бот покидает чат | |

### Удаление участника
[DOCS, DELETE /chats/{chatId}/members](https://dev.max.ru/docs-api/methods/DELETE/chats/-chatId-/members): «Удаляет участника из группового чата или канала». Бот «должен быть администратором этого чата или канала с соответствующим правом `add_remove_members`».
- `user_id` (query, int64, обязательный).
- `block` (query, bool, необязательный): «Если передать `true`, пользователь будет заблокирован в чате». Действует так: «Применяется только для чатов с публичной или приватной ссылкой. Игнорируется в остальных случаях».
- Ответ: `{"success": true|false, "message": "..."}`. **Код 200 возвращается и при неуспехе**, причина тогда указана в `message`. Проверять нужно поле `success`, а не только HTTP-код.
- **Метода для разблокировки в API нет.** Если поставить `block=true`, пользователь не сможет вернуться по ссылке даже после повторной оплаты, пока админ вручную не снимет блок в клиенте. Поэтому при истечении подписки лучше кикать с `block=false`, а повторный вход неоплативших отсекать через `user_added`.

### Может ли бот удалить участника из канала? **Нет.**
[DOCS, POST /chats/{chatId}/members/admins](https://dev.max.ru/docs-api/methods/POST/chats/-chatId-/members/admins), описание права:
> `add_remove_members` — добавлять и удалять участников группового чата или подписчиков канала. Право доступно для пользователей и в чатах, и в каналах, **для ботов — только в чатах**.

Страница DELETE формально говорит «из группового чата или канала», но описание права прямо запрещает это ботам в каналах. **Вывод: для gate нужен групповой чат (`type: "chat"`).** В канале бот может публиковать посты, но управлять подписчиками не может. Вживую это не проверено: в боте нет известного channel_id, и изменяющие вызовы запрещены условиями задачи.

### Другие различия группы и канала для бота [DOCS, admins]
- `edit_link` («изменять ссылку на групповой чат») в каналах недоступно.
- `edit` и `delete` работают только в каналах, `write` работает и в группах, и в каналах. `pin_message`, `write`, `edit` и `delete` требуют `read_all_messages`.
- `read_all_messages` нужно выдать боту, иначе webhook-обновления не придут. В интерфейсе мессенджера это право для ботов можно выдать только в групповых чатах.
- `can_call` и `view_stats` вручную не назначаются.

Права, которые нужны боту в gate-группе: `read_all_messages` и `add_remove_members`. Опционально `pin_message` и `change_chat_info`.

### Как узнать chat_id без GET /chats
[DOCS, «Получение ID чата или канала»](https://dev.max.ru/docs-api/use-cases/getting-chat-id): «Сейчас API MAX не предоставляет готовой возможности для получения списка групповых чатов и каналов, в которые добавлен бот». Для чата или канала ID приходит «только через подписку: POST /subscriptions или GET /updates — chat_id придёт в объекте Update… например, bot_added». Там же: «Использование Long Polling для получения списка чатов и каналов для бота не предусмотрено».

Следствие: chat_id нужно сохранить в момент получения `bot_added`, или задать его в конфиге вручную. Для этого его надо один раз поймать (переподключить бота в группу при работающем приёмнике событий) или получить из другого события в этой группе.

---

## 5. Инвайт-ссылки и контроль входа

- В API **нет методов для создания, отзыва или параметризации инвайт-ссылок**. В `schema.yaml` нет `invite`, `ChatPatch` не содержит поля ссылки. Есть только поле `Chat.link` («Ссылка на чат», nullable) [DOCS, объект Chat](https://dev.max.ru/docs-api/objects/Chat) и право админа `edit_link`, которое касается ссылки в клиенте.
- Аналогов `createChatInviteLink(member_limit=1, expire_date=...)` и `chat_join_request` нет.
- **[LIVE]** `GET /chats/<slug из MAX_GROUP_INVITE_LINK>`: недокументированный вариант «чат по ссылке», который использует `maxapi.get_chat_by_link`, вернул `404 chat.not.found`. Приватная ссылка вида `https://max.ru/join/<token>` в chat_id таким способом не превращается.

**Рекомендуемая схема контроля входа:**
1. Бот выдаёт оплатившему пользователю общую приватную ссылку группы (`MAX_GROUP_INVITE_LINK`). Её можно взять из конфига или из `GET /chats/{chatId}` → `link`.
2. При событии `user_added` (`chat_id` = gate-группа) бот проверяет `user.user_id` в БД. Если активной подписки нет, вызывается `DELETE /chats/{chatId}/members?user_id=...`. Опционально бот пишет человеку в личку, если тот запускал бота: «оплатите, чтобы войти».
3. Когда подписка истекает, бот вызывает `DELETE ...&block=false` и отправляет уведомление в личку.
4. Периодически бот сверяет `GET /chats/{chatId}/members` (пагинация через `marker`) с БД. Это страховка на случай пропущенных событий, например при простое webhook или потере событий в polling.
5. Если ссылка утекла, её можно сменить вручную в клиенте MAX и обновить в конфиге. Для бота это не критично, потому что неоплативших он кикает.
- Ограничение: посторонний человек успевает на короткое время оказаться в группе и увидеть свежие сообщения, пока бот не обработал событие. Полностью закрыть это окно средствами API нельзя.
- Обращайте внимание на `user_added.inviter_id`: участника мог добавить админ вручную. Такие случаи стоит отправлять в whitelist или к админу, а не кикать автоматически.

---

## 6. Платежи

В [SCHEMA] нет ничего про платежи: поиск по `pay|invoice|оплат|платеж` ничего не нашёл. В списке методов [DOCS](https://dev.max.ru/docs-api) нет аналогов `sendInvoice`, `pre_checkout_query` и `successful_payment`. **Оплата остаётся внешней:** бот отправляет кнопку `link` на платёжную форму Prodamus, Prodamus присылает webhook на наш сервер, бот выдаёт ссылку. В платёжную ссылку нужно передать MAX `user_id`, например в параметре заказа или `customer_extra`, чтобы связать платёж с пользователем.

---

## 7. Python-библиотеки

| библиотека | статус (на 2026-10-16) | что умеет |
|---|---|---|
| **[maxapi](https://github.com/love-apples/maxapi)** (love-apples), PyPI `maxapi` | v1.2.3 от 2026-10-05, 226★, push 2026-10-14, активна. Работает с `platform-api2.max.ru`, содержит CA Минцифры | «`Dispatcher`, `Router`, фильтры, `F` и middleware в стиле aiogram», «FSM-контекст через `MemoryContext` и `RedisContext`», polling и webhook (aiohttp, FastAPI, Litestar). Есть `remove_member_chat(…, block)`, `get_members_chat`, `send_callback` |
| [max-botapi-python](https://github.com/max-messenger/max-botapi-python) (официальная org) | последний push 2025-07-30, заброшена | неактуальна, старый хост |
| [umaxbot](https://github.com/Werdset/maxbot) | v0.2.0 от 2026-04-10, 4★ | «aiogram-style», маленькое сообщество |
| [aiomax](https://github.com/dpnspn/aiomax) | репозиторий **archived** | не использовать |
| Официальные SDK | только [TS](https://github.com/max-messenger/max-bot-api-client-ts) и [Go](https://github.com/max-messenger/max-bot-api-client-go) (в SCHEMA: «official Typescript client, Go client and Go framework») | официального Python SDK нет |

**Рекомендация.** API маленький: для gate нужно примерно 10 эндпоинтов, а `maxapi` не официальная и неизбежно отстаёт от изменений (например, в ней всё ещё есть `get_chats.py`). Поэтому предлагаю **свой тонкий клиент на `httpx.AsyncClient`**:
- `base_url=https://platform-api2.max.ru`
- заголовок `Authorization`
- `verify=ssl_ctx` с Russian Trusted Root CA
- rate limiter и ретраи на 429/5xx
- pydantic-модели только для нужных Update
- приём webhook в том же aiohttp или FastAPI, где уже работает приёмник Prodamus

Роутинг и FSM для админ-меню (несколько шагов: ввод тарифа, рассылка) можно сделать маленьким диспетчером по `update_type` и `callback.payload` и хранить состояние в Postgres или Redis. Если важнее скорость портирования и привычные паттерны aiogram, разумная альтернатива — `maxapi`: у неё есть Router, F-фильтры и RedisContext. Модули с бизнес-логикой (подписки, Prodamus, планировщик) при этом надо отделить от транспорта, чтобы библиотеку можно было заменить.

---

## 8. Живая проверка (2026-10-16) [LIVE]

`GET https://platform-api2.max.ru/me` (с CA Минцифры) → 200:
```json
{"user_id":514344027,"first_name":"Активируй астро-клуб","name":"Активируй астро-клуб",
 "is_bot":true,"username":"id780402906589_bot",
 "description":"Ваш помощник для доступа в астрологическую Мастерскую Марии Чернышевой",
 "last_activity_time":1792147177746,"avatar_url":"…","full_avatar_url":"…"}
```
- Ссылка на бота: `https://max.ru/id780402906589_bot`. Deep link: `https://max.ru/id780402906589_bot?start=<payload>`. Формат из [SCHEMA], ссылкой вживую я не пользовался.
- `GET /chats` → 404 `method.not.found` (метод удалён). **Список чатов бота и его админские права получить невозможно**, пока неизвестен chat_id. Его нужно поймать через `bot_added`.
- `GET /chats/<slug приватной ссылки>` → 404 `chat.not.found`.
- `GET /subscriptions` → `{"subscriptions":[]}`: webhook не настроен.
- `GET /updates?timeout=0&limit=1` → `{"updates":[],"marker":83010}`: очередь пуста. Большой marker говорит о том, что раньше бот получал события. Чтобы не перехватить события у работающего экземпляра, больше я polling не вызывал.
- Токен в query → 401 deprecated. Старый хост `platform-api.max.ru` пока отвечает 200.

---

## 9. Подводные камни

1. **Deep link (аналог `/start payload`).** [SCHEMA]: «Each bot has start link that looks like: `https://max.ru/<botName>?start=<payload>`… we open dialog with bot and send this payload to bot as part of `bot_started` update». Ограничение: «up to **128** characters long. Longer strings will be omitted and **not** passed to the bot». Работает на iOS 2.7.0+ и Android 2.9.0+.
   - `bot_started` по документации приходит, когда пользователь «впервые начал общение с ботом или возобновил после остановки». Сообщение `/start` от пользователя, который уже общается с ботом, приходит как обычный `message_created` с текстом `/start`, без payload. **Если пользователь уже в диалоге с ботом, payload может не дойти.** Вывод получен из определения события, вживую не проверен. Нужно обрабатывать и `bot_started`, и `message_created` с текстом `/start`.
   - Схема payload: только `[A-Za-z0-9_-]` (безопасный набор; явный charset для `start=` в документации не указан).
2. **ID типа int64.** `user_id` у MAX отличается от Telegram: например, 514344027 у бота. ID групп отрицательные. Колонки в БД должны быть `BIGINT`. Telegram-ID в MAX не переносятся, пользователи должны заново пройти через бота.
3. **В личку можно писать только тем, кто запускал бота.** Если пользователь нажал «остановить» (`bot_stopped` / `dialog_removed`), напоминания до него не дойдут. Это аналог `TelegramForbiddenError`. Отслеживать статус нужно по этим событиям.
4. **Ответ 200 ещё не означает успех.** Методы, возвращающие `SimpleQueryResult` (DELETE members, POST /answers и др.), отдают `{"success": false, "message": "..."}` с кодом 200.
5. **Webhook молча отписывается** после 8 часов недоставок. Нужен health-check через `GET /subscriptions` с переподпиской.
6. **TLS Минцифры** нужен и в Docker-образе (python:slim его не содержит). Сертификат кладётся в репозиторий или образ и подключается через `SSLContext`. Sub CA истекает 2027-03-06, доверять надо Root.
7. **Время в миллисекундах:** `timestamp`, `last_activity_time`, `join_time`.
8. **Нет отложенной отправки** (`send_at`). Напоминания делаем своим планировщиком, как и сейчас (APScheduler или cron по БД).
9. **Длина текста ≤4000 символов**, лимит считается в символах, разметка входит в подсчёт [SKILL, со ссылкой на SCHEMA `maxLength`].
10. **Бот должен быть админом** с `read_all_messages` и `add_remove_members` **в групповом чате**. Канал для gate не подходит, см. §4.
11. **`POST /chats/{id}/members` ограничен.** Добавить оплатившего в группу API-вызовом нельзя, только по ссылке.

---

## Источники
- Обзор API и список методов: https://dev.max.ru/docs-api
- История изменений API: https://dev.max.ru/docs-api/changelog-api
- Подготовка (30 rps, базовый URL): https://dev.max.ru/docs/chatbots/bots-coding/prepare
- GET /updates: https://dev.max.ru/docs-api/methods/GET/updates
- POST /subscriptions: https://dev.max.ru/docs-api/methods/POST/subscriptions
- Объект Update: https://dev.max.ru/docs-api/objects/Update
- Объект Chat: https://dev.max.ru/docs-api/objects/Chat
- POST /answers: https://dev.max.ru/docs-api/methods/POST/answers
- DELETE members: https://dev.max.ru/docs-api/methods/DELETE/chats/-chatId-/members
- POST members: https://dev.max.ru/docs-api/methods/POST/chats/-chatId-/members
- Права админов: https://dev.max.ru/docs-api/methods/POST/chats/-chatId-/members/admins
- Получение chat_id: https://dev.max.ru/docs-api/use-cases/getting-chat-id
- Бот в чатах и каналах: https://dev.max.ru/docs-api/use-cases/bot-in-chats-and-channels
- OpenAPI: https://github.com/max-messenger/api-schema/blob/main/schema.yaml
- Agent Skill (официальная выжимка): https://github.com/max-messenger/max-agents-skill
- maxapi: https://github.com/love-apples/maxapi, https://pypi.org/project/maxapi/
- CA Минцифры: https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer
