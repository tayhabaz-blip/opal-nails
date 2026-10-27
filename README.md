# Nail Studio by Opal Ben Moha

אתר קביעת תורים לסטודיו של אופל.

- `docs/` — האתר עצמו (עמוד אחד), מתפרסם בגיטהאב פייג׳ס.
- `server/` — השרת של התורים, רץ ברנדר.

## הגדרות השרת ברנדר

| הגדרה | ערך |
|---|---|
| Build Command | `pip install -r server/requirements.txt` |
| Start Command | `cd server && uvicorn app:app --host 0.0.0.0 --port $PORT` |
| Disk | Mount path `/var/data`, 1 GB |

משתני סביבה:

| שם | מה זה |
|---|---|
| `DB_PATH` | `/var/data/opal.db` |
| `ADMIN_PASSWORD` | הסיסמה של אופל לפאנל הניהול |
| `SECRET_KEY` | לא חובה. אם לא מוגדר, השרת יוצר אחד בעצמו ושומר אותו בדיסק |
| `ALLOWED_ORIGINS` | לא חובה. ברירת המחדל מתאימה לכתובות של האתר |
| `SMS_PROVIDER` | `log` עד שיש ספק הודעות |

## ניהול

פאנל הניהול נמצא בכתובת האתר עם `#admin` בסוף.

## בדיקות

```
cd server
pip install -r requirements.txt pytest httpx
python -m pytest tests
```
