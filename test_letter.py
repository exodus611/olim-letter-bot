"""
Проверка разбора без Telegram: прогоняет один файл через Gemini + базу знаний и печатает,
что увидит пользователь.

  python test_letter.py путь/к/письму.jpg
  python test_letter.py --demo      # сгенерирует и разберёт тестовое письмо Битуах Леуми

Нужен GEMINI_API_KEY в .env.
"""
import asyncio
import html
import re
import sys
from pathlib import Path

from analyzer import analyze_letter, format_contacts, format_glossary, format_main, format_reply_draft


def strip_html(s: str) -> str:
    return html.unescape(re.sub(r"</?(b|i|code)>", "", s))


def make_demo_letter(path: Path) -> None:
    """Рисует правдоподобное письмо Битуах Леуми о долге (текст на иврите) для теста."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
        bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
    except OSError:
        font = bold = ImageFont.load_default()

    def rtl(text, y, f=font, x=W - 80):
        # DejaVu не делает RTL-шейпинг, поэтому просто разворачиваем строку — для OCR моделью этого достаточно
        d.text((x, y), text[::-1], fill="black", font=f, anchor="ra")

    rtl("המוסד לביטוח לאומי", 80, bold)
    rtl("סניף תל אביב, רחוב יצחק שדה 17", 140)
    rtl("תאריך: 25.08.2026", 220)
    rtl("מספר זהות: 3XXXXXXXX", 260)
    rtl("מספר תיק: 71-4455-889", 300)
    rtl("הנדון: הודעה על חוב בגין תשלום יתר של דמי אבטלה", 380, bold)
    rtl("שלום רב,", 460)
    rtl("מבדיקה שערכנו עולה כי בתקופה 03/2026 - 05/2026 שולמו לך דמי אבטלה", 520)
    rtl("ביתר, בסך 4,860 ש\"ח, מאחר שדווחה הכנסה מעבודה בתקופה זו.", 560)
    rtl("עליך להסדיר את החוב בתוך 30 יום מיום קבלת מכתב זה.", 640)
    rtl("ניתן לשלם באתר, או להגיש בקשה לפריסת תשלומים.", 680)
    rtl("אם אינך מסכים/ה לקביעה זו, באפשרותך להגיש השגה בכתב בצירוף מסמכים.", 740)
    rtl("אי הסדרת החוב תגרור קיזוז מקצבאות עתידיות והעברה לגבייה.", 820)
    rtl("בכבוד רב,", 920)
    rtl("מחלקת גבייה, סניף תל אביב", 960)
    d.rectangle([60, 1500, W - 60, 1620], outline="black", width=2)
    rtl("שובר לתשלום  |  סכום: 4,860 ש\"ח  |  לתשלום עד: 24.09.2026", 1540)
    img.save(path, quality=92)


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "--demo":
        path = Path("demo_letter.jpg")
        make_demo_letter(path)
        print(f"Сгенерировано тестовое письмо: {path}")
    else:
        path = Path(sys.argv[1])

    mime = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    print("Разбираю…\n")
    r = await analyze_letter(path.read_bytes(), mime)

    print("=" * 70)
    print(strip_html(format_main(r)))
    print("\n" + "=" * 70)
    print(strip_html(format_contacts(r)))
    for block in (format_reply_draft(r), format_glossary(r)):
        if block:
            print("\n" + "=" * 70)
            print(strip_html(block))
    print("\n" + "=" * 70)
    print("Служебное: org_id =", r["org_id"], "| letter_type =", r["letter_type"], "| deadline =", r["deadline"],
          f"({r['deadline_basis']})", "| days_left =", r["days_left"], "| scam =", r.get("scam_risk"),
          "| specialist =", r.get("specialist_type"))


if __name__ == "__main__":
    asyncio.run(main())
