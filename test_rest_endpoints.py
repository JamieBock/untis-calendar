import os
import requests
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

server = os.getenv("WEBUNTIS_SERVER")
school = os.getenv("WEBUNTIS_SCHOOL")
username = os.getenv("WEBUNTIS_USERNAME")
password = os.getenv("WEBUNTIS_PASSWORD")

print(f"Testing Untis REST endpoints on {server} / {school}")

# Basis-Endpunkte (neues REST-System)
base_url = f"https://{server}/WebUntis/api/rest/view/v1"

# Login-Session mit normalem Login-Endpunkt (Perseus-kompatibel)
session = requests.Session()
login_url = f"https://{server}/WebUntis/api/rest/auth/login"
payload = {"school": school, "j_username": username, "j_password": password}
r = session.post(login_url, json=payload, timeout=15)
print("Login status:", r.status_code)
if r.status_code != 200:
    print("❌ Login fehlgeschlagen – REST-Modul evtl. deaktiviert.")
    exit()

today = date.today()
end = today + timedelta(days=7)

# Hausaufgaben
hw_url = f"{base_url}/homeworks?resourceType=STUDENT&timetableType=MY_TIMETABLE&start={today.isoformat()}&end={end.isoformat()}"
resp_hw = session.get(hw_url, timeout=20)
print("Hausaufgaben:", resp_hw.status_code)
print(resp_hw.text[:500])

# Prüfungen
exam_url = f"{base_url}/exams?resourceType=STUDENT&timetableType=MY_TIMETABLE&start={today.isoformat()}&end={end.isoformat()}"
resp_ex = session.get(exam_url, timeout=20)
print("Prüfungen:", resp_ex.status_code)
print(resp_ex.text[:500])

session.close()
