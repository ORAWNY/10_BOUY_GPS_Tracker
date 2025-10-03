# fake_rockblock_sender_ew_imei.py
import time
import json
from datetime import datetime, timezone, timedelta
import requests

BASE = "http://127.0.0.1:5000"
INGEST = f"{BASE}/ingest"

def iso_now(offset_s=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")

momsn = int(time.time()) % 60000
def next_momsn():
    global momsn
    momsn = (momsn + 1) % 65536
    return momsn

def ts12():
    return datetime.utcnow().strftime("%y%m%d%H%M%S")

EW = {
    "300534068522890": {  # EW1_L1
        "fields": ["Log_no","C_S","Source","date","BATTERY","AMRW-com","AMRWBar","AMRWTemp",
                   "AMRWDirT","AMRSpd","Max_Spd","VelStdDev","Gust_Factot","3_Sec_Gust",
                   "Vector_AVG_SPEED","Lat","Lon","GPStime"],
        "values":  ["100",   "L1", "EW1",  ts12(),  "12.3",   "180",     "1013.2",  "15.6",
                    "175",     "8.2",    "12.5",    "0.7",        "1.3",       "15.9",
                    "7.4",             "52.1234","-6.5432", ts12()]
    },
    "300534064793870": {  # EW1_L3
        "fields": ["Log_no","C_S","Source","date","Battery","Battery1","Battery2",
                   "SystemV","Sp1","Sp2","Sp3","Sp4","Lat","Lon","Sp5","Sp6"],
        "values":  ["101",   "L3", "EW1",  ts12(), "12.0",  "12.1",   "12.2",
                    "12.3", "1.1","1.2","1.3","1.4","52.2000","-6.6000","1.5","1.6"]
    },
    "300534064799870": {  # EW1_MOS2_SBC1_
        "fields": ["Log_no","C_S","Source","date","BATTERY","AMRW-com","AMRWBar","AMRWTemp",
                   "AMRWDirT","AMRSpd","Max_Spd","VelStdDev","Gust_Factot","3_Sec_Gust",
                   "Vestor_AVG_SPEED","col11","col12","col13"],
        "values":  ["102",   "MOS2","EW1", ts12(),  "11.8",   "182",     "1012.9",  "14.8",
                    "177",     "9.0",    "13.2",    "0.6",        "1.2",       "16.5",
                    "8.0",             "x11","x12","x13"]
    },
    "300534068527890": {  # EW1_L9
        "fields": ["Log_no","C_S","Source","date","BATTERY_A","Battery_B"],
        "values":  ["103",   "L9", "EW1",  ts12(), "12.5",     "12.2"]
    },
    "300534068523870": {  # EW1_L2
        "fields": ["Log_no","C_S","Source","date","BATTERY_A","Battery_B",
                   "Col_7","col_8","col9","col10","col11","col12","col13","col14"],
        "values":  ["104",   "L2", "EW1",  ts12(), "12.4",     "12.0",
                    "7","8","9","10","11","12","13","14"]
    },
}

def make_s_line(values):
    return "#S," + ",".join(values) + ",**"

def post_one(imei, data_text):
    body = {
        "imei": imei,                          # << include IMEI
        "momsn": next_momsn(),
        "received_utc": iso_now(0),
        "transmit_time_utc": iso_now(-5),
        "data_text": data_text
        # no 'sender' here; parser will synthesize imei@rockblock.rock7.com
    }
    r = requests.post(INGEST, json=body, timeout=10)
    r.raise_for_status()
    return r.json()

def main():
    print("Posting EW IMEI test messages to", INGEST)
    for imei, spec in EW.items():
        line = make_s_line(spec["values"])
        print(f"→ IMEI {imei} :: {line}")
        try:
            resp = post_one(imei, line)
            print("  OK:", json.dumps(resp, indent=2))
        except Exception as e:
            print("  ERROR:", e)
        time.sleep(0.2)

if __name__ == "__main__":
    main()
