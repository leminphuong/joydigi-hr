# OKR Chat API (`/api/okr/chat/`, 2026-11-09, updated 2026-11-11)

Same conventions as the rest of `/api/okr/`:
- **Authentication**: the app's JWT (`Authorization: Bearer …`) or the HR web
  session. With a session, every unsafe method needs `X-CSRFToken`.
- **Errors**: always `{"code", "message", "fields"}` with a Vietnamese
  `message`.
- **Not found**: a room, message or file outside the caller's company, or one
  they may not use, answers `404 not_found`. The id is not leaked.
- **Feature flag**: while `OKR_CHAT_ENABLED` is off, every endpoint answers
  `404 chat_disabled`.
- **Pilot group** (2026-11-11): `OKR_CHAT_PILOT_EMPLOYEE_IDS` (comma-separated
  employee ids). When it is set, only those people have chat:
  - for everyone else every endpoint answers `404 chat_disabled`;
  - the web menu and page are hidden and the room page answers 404;
  - nobody outside the group is listed in `members/` (the @ picker), kept as
    a mention, notified or pushed.

  Empty (the default) means everyone in the company, as before. The flag only
  narrows: it never grants access to a room.

Who may use a room is checked from live HR and OKR data on every request;
the rules are in the docstring of `okr/services/chat.py`.

## Rooms

### `GET rooms/?kind=&q=`
The caller's rooms, newest activity first (at most 200):
- their Team rooms;
- their department room;
- their 1–1 rooms;
- the OKR rooms they opened.

Team and department rooms are created on first listing. Each row:

```json
{
  "id": 41, "kind": "team|department|direct|objective|key_result",
  "title": "Team A",
  "target": {"type": "team", "team_id": 3, "owner": {"id": 2, "name": "…", "avatar": "/media/…"}},
  "partner": null,
  "unread": 2, "muted": false, "last_read_id": 120,
  "last_message_at": "…",
  "last_message": {"id": 122, "sender_name": "…", "excerpt": "…", "created_at": "…"},
  "permissions": {"post": true, "moderate": false}
}
```

For a key result, `target` also carries `key_result_id`, `objective_id` and `objective_title`. `partner` is the other person of a 1–1 room.

### `POST rooms/`
Open or find one room. The call is idempotent: the same target always gives the same room.

| Body | Room |
|---|---|
| `{"kind": "objective", "objective_id": 12}` | Objective |
| `{"kind": "key_result", "key_result_id": 5}` | Key result |
| `{"kind": "team", "team_id": 3}` | Team |
| `{"kind": "department", "department_id": 2}` | Department |
| `{"kind": "direct", "employee_id": 9}` | 1–1 (same company only) |

Answers:

| Status / code | When |
|---|---|
| 200 | The room, in the shape above |
| `403 chat_not_member` | The caller may see the OKR but is not in its Team / department |
| 404 | Anything they may not see |

### `GET rooms/{id}/`
One room, in the same shape.

### `GET rooms/{id}/members/?q=`
People who may use the room, at most 50: `{"count", "results": [{"id", "name", "avatar"}]}`. Used for the member list and the @ picker.

### `POST rooms/{id}/read/`
Body `{"message_id"}`. Moves the read position forward (never back) and settles the chat notifications up to that message.

Answer: `{"room_id", "last_read_id", "unread", "rooms"}`.

### `POST rooms/{id}/mute/`
Body `{"muted": true|false}`. A muted room only pushes mentions.

## Messages

### `GET rooms/{id}/messages/`
Results are always oldest first.

| Query | Returns |
|---|---|
| none | the latest 30 |
| `before=<id>` | the 30 before (scrolling up) |
| `after=<id>` | up to 100 newer ones (polling) |
| `after=<id>&changed_since=<server_time>` | also `changed`: messages up to `after` edited or recalled since that time |
| `q=` | up to 50 live matches; accent-insensitive (`khuyen mai` finds "khuyến mãi") |

```json
{"room_id": 41, "results": [MESSAGE], "changed": [MESSAGE],
 "has_more_before": true, "has_more_after": false, "server_time": "2026-11-09T09:00:00Z"}
```

`MESSAGE`:

```json
{
  "id": 122, "room_id": 41, "client_message_id": "only on your own messages",
  "sender": {"id": 2, "name": "…", "avatar": "/media/…"}, "is_mine": false,
  "content": "text (empty when recalled)", "created_at": "…", "edited_at": null, "updated_at": "…",
  "recalled": false, "recalled_by_moderator": false,
  "reply_to": {"id": 120, "sender_name": "…", "excerpt": "…", "recalled": false},
  "attachments": [{"id": 7, "name": "anh.png", "size": 1024, "content_type": "image/png", "is_image": true,
                   "download_url": "/api/okr/chat/attachments/7/download/"}],
  "mentions": [{"id": 9, "name": "…"}],
  "permissions": {"edit": true, "recall": true}
}
```

`content` is plain text, stored as typed. Clients must escape it and never render it as HTML.

### `POST rooms/{id}/messages/`

```json
{"client_message_id": "8–64 of [A-Za-z0-9_-]", "content": "≤ 4000 characters",
 "reply_to": 120, "attachment_ids": [7], "mention_ids": [9]}
```

Answers:

| Status / code | When |
|---|---|
| 201 | Message created |
| 200 | Same `client_message_id` sent again by the same person in the same room: the first copy, no duplicate |
| `400 validation_error` | Empty text without files, text too long, a reply from another room, or files not uploaded by the caller to this room |
| `403 chat_read_only` | The other person of a 1–1 room left the company |
| `429 throttled` | More than 20 messages a minute |

- **Sender**: always the caller; any `sender` or `sender_id` in the body is ignored.
- **Mentions**: people who may not use the room are dropped silently.

### `PATCH messages/{id}/`
Body `{"content"}`. Only for your own message, not recalled, within 24 hours.
- Someone else's message: `403`.
- Too late: `400 chat_edit_window`.

### `DELETE messages/{id}/` (recall)
Who may recall:
- the sender, at any time;
- a Team / department manager, in their room;
- an OKR admin, in OKR rooms.

Nobody can recall someone else's message in a 1–1 room. After a recall, the text and the files are removed for everyone. Answer: the message with `recalled: true`.

## Files

### `POST attachments/` (multipart)
Fields `room_id` and `file`. Allowed files:
- 10 MB at most;
- jpg, png, webp, pdf, doc, docx, xls, xlsx;
- the content must match the extension.

Content checks (`okr/services/chat_files.py`, 2026-11-11), any failure is `400 validation_error`:
- **Images** are fully decoded with Pillow. The real format must match the extension, at most 40 million pixels.
- **docx/xlsx** must be a real Office package. Refused: macros (`vbaProject.bin`), ActiveX, more than 3000 entries, more than 200 MB unpacked, or a compression ratio above 200.
- **doc/xls** (old format): refused when the file carries macro streams.
- **PDF**: refused when it carries `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile` or `/RichMedia`.
- **Names** are cleaned: no path, no control or bidi characters, at most 255 characters.

Limits: 10 uploads a minute, and at most 20 not yet sent. Answer `201`: the attachment, which goes into `attachment_ids` of the next message in that room.

### `DELETE attachments/{id}/`
Drops one of the caller's own files that is not sent yet (the draft was cancelled). Answer `204`.

A file that is already sent, or anyone else's file, answers `404`. Sent files only go away with a recall.

### `GET attachments/{id}/download/?inline=1`
The file, from private storage (never `/media/`). Only for people who may use the room. A file not yet sent is visible to its uploader only.

`inline=1` shows images in place; everything else is a download. Headers:
- `Cache-Control: private, no-store`;
- `X-Content-Type-Options: nosniff`;
- `Content-Security-Policy: default-src 'none'; sandbox`.

## Other

### `GET unread-count/`
`{"unread": 5, "rooms": 2}`. Other people's live messages after the caller's read position, in rooms they may use.

### `GET search/?q=`
Up to 50 live messages matching `q` (at least 2 characters) in the caller's rooms:
`{"count", "results": [{"room_id", "room_kind", "room_title", "message": MESSAGE}]}`.

## Notifications and push

### In-app notifications
`/api/okr/notifications/` lists `chat_mention` and `chat_reply` with `reference: {"type": "chat_message", "id", "available", "room_id"}`.
- `available` turns false, and `room_id` becomes null, once the caller can no longer open that room.
- The title says who did what, never the message text.

### Push
Firebase push through `joydigi_api.push.send_to_user`, after commit, in a background thread.

| Room | Who is pushed |
|---|---|
| 1–1 | the other person |
| Team / department | every member |
| OKR | people who joined the room |
| Any room | mentioned people, the author of the replied message |

Rules:
- Each recipient's access is checked again right before sending.
- Pushes respect `NotificationPreference.all_notifications_enabled`.
- A plain message reaches a person at most once per room every 5 minutes. Mentions and replies always go out.
- Body: `"<sender> · Tin nhắn mới (Team)"`, or "đã nhắc đến bạn" / "đã trả lời tin nhắn của bạn". **No message text.**
- Payload: `type=okr_chat`, `room_id`, `message_id`, `reason`.

## Cleaning up files never sent

```
python manage.py okr_chat_cleanup_uploads [--older-than-hours 24] [--company ID] [--limit 500]
python manage.py okr_chat_cleanup_uploads --apply --expect-count N
```

- **Dry run by default**: it lists `UNSENT attachment=… company=… uploader=… size=… uploaded=…`. It never prints file names.
- **`--apply`** needs `--expect-count`, the number the dry run just showed. It stops when the count differs.
- **Only** files not attached to any message and older than the grace period (at least 1 hour, 24 by default). Each row is locked and re-checked before deletion. The file is removed from disk only after the database commit.

Nothing schedules it: run it by hand, or from cron once approved.

## Not in this version
- WebSocket or other realtime transport: clients poll. The service layer is the only writer, ready for Channels later.
- Voice or video.
- A scheduled cleanup job (the command above is run by hand).
