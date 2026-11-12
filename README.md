# გამოცდის ადგილების მონიტორი (my.sa.gov.ge)

ამოწმებს **მართვის თეორიულ გამოცდას, ცენტრი რუსთავი** ყოველ 5 წუთში და ახალი თარიღის/დროის გამოჩენისას Telegram-ზე გიგზავნის შეტყობინებას.

## 1. Telegram ბოტი
1. Telegram-ში გახსენი **@BotFather** → `/newbot` → მიიღებ ტოკენს.
2. ჩასვი `.env`-ში: `TELEGRAM_BOT_TOKEN=...`
3. შენს ახალ ბოტს მიწერე ნებისმიერი რამ, მერე გაუშვი: `python exam_monitor.py --chat-id`
4. გამოსული რიცხვი ჩასვი `.env`-ში: `TELEGRAM_CHAT_ID=...`
5. შემოწმება: `python exam_monitor.py --test-telegram`

## 2. მეილის კოდი
ლოგინისას საიტი მეილზე კოდს აგზავნის. ორი ვარიანტი:
- **ავტომატური:** Gmail-ზე ჩართე 2-Step Verification, შექმენი App password
  (https://myaccount.google.com/apppasswords) და ჩასვი `GMAIL_APP_PASSWORD=`-ში.
- **ხელით:** თუ ცარიელს დატოვებ, ბოტი Telegram-ში მოგწერს და კოდს იქ გაუგზავნი.

ტოკენი ინახება `.token.json`-ში, ასე რომ კოდი მხოლოდ ტოკენის ვადის გასვლისას დაჭირდება.

## 3. გაშვება
```
pip install -r requirements.txt
python exam_monitor.py --discover   # ერთჯერადი შემოწმება — აჩვენებს რას აბრუნებს საიტი
python exam_monitor.py              # უწყვეტი მონიტორინგი (ან ორჯერ დააწკაპე run.bat)
```
კომპიუტერი ჩართული უნდა იყოს, სანამ მონიტორი მუშაობს.
