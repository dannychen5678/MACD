import requests
import pandas as pd
import time
from datetime import datetime, timedelta
from flask import Flask
import threading


# === Telegram 設定 ===
# 這裡是用來「發通知」給 Telegram 的設定。
# 你可以想像成：程式一發現行情有異常，就會自動傳訊息到你 Telegram。
# === Telegram 設定 ===
BOT_TOKEN = "8262097219:AAGEtNSYY81GrtupVILIxqTA2rnt7Z0woUo" #創的bot token
CHAT_ID = "8414393276" #update的chat id
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# === 台指期 即時報價資料 來源 ===
# 這是台灣期交所（TAIFEX）的官方 API，可以抓到最新台指期報價。
# === 台指期即時行情 URL & Payload ===
URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"

#切換交易時段
def get_market_type():
    now = datetime.now().time()

    # 一般日盤：08:45–13:45
    if datetime.strptime("08:45", "%H:%M").time() <= now <= datetime.strptime("13:45", "%H:%M").time():
        return "0"

    # 盤後交易：15:00–05:00（跨夜）
    # 分兩段判斷：15:00–23:59 或 00:00–05:00
    if now >= datetime.strptime("15:00", "%H:%M").time() or now <= datetime.strptime("05:00", "%H:%M").time():
        return "1"

    # 其他時間沒有行情，維持日盤模式即可
    return "0"

# 這個 function 負責準備 API 要的「查詢格式」
def get_payload():  
    return {
        "MarketType": get_market_type(),  # 盤後交易時段的payload 1 ,一般交易時段要改成0
        "SymbolType": "F", # F 代表期貨
        "KindID": "1",
        "CID": "TXF",# 台指期的代號
        "ExpireMonth": "",      
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A"
    }
#自我保持運作
def keep_alive(url):
    while True:
        try:
            requests.get(url)
            print("Pinged self to stay awake")
        except:
            pass
        time.sleep(600)  # 每 10 分鐘 ping 一次

# 發送通知給 Telegram（例如出現背離的時候）
def send_alert(msg):
    requests.post(API_URL, data={"chat_id": CHAT_ID, "text": msg})

# 抓取最新成交價
def fetch_latest_price():
    try:
        r = requests.post(
            URL,
            json=get_payload(),
            headers={"Content-Type": "application/json"}
        )
        data = r.json()
        quotes = data.get("RtData", {}).get("QuoteList", [])
        if not quotes:
            print("⚠️ 沒有取得 QuoteList,可能尚未開盤或伺服器暫無資料。")
            return None, None, None

        txf_list = [q for q in quotes if q["SymbolID"].startswith("TXF") and q["CLastPrice"]]
        if not txf_list:
            print("⚠️ 找不到近月台指期報價。")
            return None, None, None

        q = txf_list[0]
        price = float(q["CLastPrice"])
        ref_price = float(q["CRefPrice"]) if q["CRefPrice"] else price
        timestamp = datetime.now()
        
        return timestamp, price, ref_price

    except Exception as e:
        print("❌ 抓取成交價失敗:", e)
        return None, None, None

# === MACD 計算 ===
# MACD 是技術指標，用來判斷「多空動能」。
# 它有兩條線：快線 (短期趨勢) 與慢線 (長期趨勢)。
# 當快線向上穿過慢線 → 看多訊號。
# 當快線向下穿過慢線 → 看空訊號
def calc_macd(df):
    short = df['close'].ewm(span=12, adjust=False).mean()# 短期平均線
    long = df['close'].ewm(span=26, adjust=False).mean()## 長期平均線
    df['MACD'] = short - long # 快線 - 慢線
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()  # 訊號線
    df['MA5'] = df['close'].rolling(window=5).mean()
    df['MA10'] = df['close'].rolling(window=10).mean()
    df['MA20'] = df['close'].rolling(window=20).mean()
    return df

# === 自動判斷「觀察範圍」大小 ===
# 根據最近的波動情況，來決定要回頭看幾根 K 線。
# 例如波動大 → 看長一點；波動小 → 看短一點。
def adaptive_lookback(df, base_min=5, base_max=15):
    """
    根據波動幅度自動調整 lookback，上下界微調。
    base_min / base_max 為基準 lookback。
    回傳: lb, vol
    """
    if len(df) < 2:
        return base_min, 0

    # 取最近 10 根完整 K 線計算波動幅度,10根以下太短,樣本太小波動不穩定,10根以上的話反應太慢可能無法抓到最新波動變化
    recent = df['close'].tail(10)
    vol = recent.max() - recent.min()

    # 動態上下界
    # 小波幅 -> 縮短 min_lb
    # 大波幅 -> 拉長 max_lb
    min_lb = max(3, base_min + int((vol - 50)/100))
    max_lb = base_max
    if vol > 150:
        max_lb = base_max + 5

    # 計算自動 lookback
    #如果
    if vol < 50:
        lb = min_lb
    elif vol > 150:
        lb = max_lb
    else:
        lb = min_lb + int((vol - 50) / (150 - 50) * (max_lb - min_lb))

    return lb, vol

# === 判斷是否出現「MACD 背離」 ===
# 背離的意思：價格一直創高，但 MACD 沒跟著創高（或相反）
# → 通常代表市場的動能「在減弱」，可能即將反轉。
"""
def check_divergence(df):
    if len(df) < 26:
        return None
    
    lb, _ = adaptive_lookback(df)
     # 取最近幾根 K 線的資料
    recent = df['close'].iloc[-lb:]
    macd_recent = df['MACD'].iloc[-lb:]
    signal_recent = df['Signal'].iloc[-lb:]
     # 判斷價格方向
    price_diff = recent.diff().dropna()
    # 價格要「連續 5 根全部上漲」或「連續 5 根全部下跌
    if all(price_diff > 0): # 價格一直漲
        price_dir = 1
    elif all(price_diff < 0):# 價格一直跌
        price_dir = -1
    else:
        return None # 沒有明顯方向，不算
    # 判斷 MACD 方向
    macd_diff = macd_recent.diff().dropna()
    if all(macd_diff > 0):
        macd_dir = 1
    elif all(macd_diff < 0):
        macd_dir = -1
    else:
        return None
     # 判斷 MACD 是否在同一區域（全正或全負）  
    macd_color = macd_recent - signal_recent
    if not (all(macd_color > 0) or all(macd_color < 0)):
        return None
    # 真正的背離條件：
    # 價格創新高但 MACD 在掉 → 頂部背離（可能要跌）
    # 價格創新低但 MACD 在升 → 底部背離（可能要漲）
    if price_dir == 1 and macd_dir == -1:
        return "頂部背離,看空警示"
    elif price_dir == -1 and macd_dir == 1:
        return "底部背離,看多警示"
    
    return None
    """
"""
# === 判斷是否出現「MACD 背離」(改良版) ===
def check_divergence(df, consecutive=3, threshold=1):
    """
    df: 已計算好 MACD 的 K 線 DataFrame
    consecutive: 連續幾根 K 線才算趨勢
    threshold: 容忍每根 K 線小幅回落或回升
    """
    if len(df) < 26:
        return None
    
    lb, _ = adaptive_lookback(df)
    recent = df['close'].iloc[-lb:]
    macd_recent = df['MACD'].iloc[-lb:]
    signal_recent = df['Signal'].iloc[-lb:]

    price_diff = recent.diff().dropna()
    
    # 判斷價格方向（允許小幅回調）
    price_dir = 0
    for i in range(len(price_diff) - consecutive + 1):
        window = price_diff.iloc[i:i+consecutive]
        # 如果全部大於 -threshold → 算上升
        if (window > -threshold).all():
            price_dir = 1
            break
        # 如果全部小於 threshold → 算下降
        elif (window < threshold).all():
            price_dir = -1
            break
    
    if price_dir == 0:
        return None  # 沒有明顯方向

    # 判斷 MACD 方向（仍要求連續，暫不允許回調）
    macd_diff = macd_recent.diff().dropna()
    if all(macd_diff > 0):
        macd_dir = 1
    elif all(macd_diff < 0):
        macd_dir = -1
    else:
        return None

    # 判斷 MACD 是否在同一區域（全正或全負）
    macd_color = macd_recent - signal_recent
    if not (all(macd_color > 0) or all(macd_color < 0)):
        return None

    # 背離條件
    if price_dir == 1 and macd_dir == -1:
        return "頂部背離,看空警示"
    elif price_dir == -1 and macd_dir == 1:
        return "底部背離,看多警示"
    
    return None
"""
def check_divergence(df, consecutive=3, threshold=1):
    if len(df) < 60:
        return None

    # 動態 lookback
    lb, _ = adaptive_lookback(df)

    recent = df['close'].iloc[-lb:]
    prev = df['close'].iloc[-lb*2:-lb]

    macd_recent = df['MACD'].iloc[-lb:]
    signal_recent = df['Signal'].iloc[-lb:]
    macd_diff = macd_recent.diff().dropna()

    # ========= ①價格是否創高/創低 =========
    high_now = recent.max()
    low_now = recent.min()
    high_prev = prev.max()
    low_prev = prev.min()

    if high_now > high_prev:
        price_dir = 1   # 價格創高 → 可能頂部背離
    elif low_now < low_prev:
        price_dir = -1  # 價格創低 → 可能底部背離
    else:
        return None

    # ========= ②MACD 趨勢允許 30% 回調 =========
    pos = (macd_diff > 0).sum()
    neg = (macd_diff < 0).sum()

    if pos >= len(macd_diff)*0.7:
        macd_dir = 1
    elif neg >= len(macd_diff)*0.7:
        macd_dir = -1
    else:
        return None

    # ========= ③MACD 顏色（允許部分交錯） =========
    macd_color = macd_recent - signal_recent
    pos_color = (macd_color > 0).sum()
    neg_color = (macd_color < 0).sum()

    if not (pos_color >= lb*0.7 or neg_color >= lb*0.7):
        return None

    # ========= ④背離判斷 =========
    if price_dir == 1 and macd_dir == -1:
        return "頂部背離,看空警示"

    if price_dir == -1 and macd_dir == 1:
        return "底部背離,看多警示"

    return None

# === 主程式 ===
def main():
    print("🔍 開始監控台指期 MACD 背離訊號...")
    # 這個 DataFrame 是在收集每一筆即時報價（類似逐筆成交）
    df_tick = pd.DataFrame(columns=['Close'])
    last_alert = None # 上次發送通知的時間
    last_alert_time = datetime.min  # Telegram 冷卻時間
    cooldown = timedelta(minutes=5)  # 同方向訊號 5 分鐘內不重複推播
    ref_price = None # 用來記錄開盤價或昨日參考價
    
     # 不停重複執行（即時監控）
    while True:
        timestamp, price, current_ref = fetch_latest_price()
        if price:
             # 第一次抓到參考價時記下來
            if current_ref and not ref_price:
                ref_price = current_ref
            
            # 確保 index 是時間格式
            df_tick.index = pd.to_datetime(df_tick.index, errors='coerce')

            # 保留最近 15 小時資料
            cutoff_time = datetime.now() - timedelta(hours=15)
            df_tick = df_tick.loc[df_tick.index >= cutoff_time]

             # 把這一筆價格記錄進去（附時間）
            df_tick.loc[timestamp] = price
            
            # # 把逐筆價重新整理成「5 分鐘 K 線」
            df_5min = df_tick['Close'].resample('5T').ohlc()# 開高低收
            df_5min['volume'] = df_tick['Close'].resample('5T').count()# 每5分鐘成交次數
            df_5min.dropna(inplace=True) #最後才dropna
             # 顯示目前狀況
            print(f"📈 {timestamp.strftime('%H:%M:%S')} | 價格: {price} | Tick數: {len(df_tick)} | K線: {len(df_5min)}根")
             # 至少要有 26 根 K 線才能算 MACD（因為慢線是 26 期平均）
            if len(df_5min) >= 26:
                df_5min = calc_macd(df_5min)
                # 排除最後一根「尚未結束的 K 線」，避免半根線誤判
                df_complete = df_5min.iloc[:-1]

                # 這裡會計算最近波動幅度，並印出 Debug 訊息
                lb, vol = adaptive_lookback(df_complete)
                print(f"📊 Debug: 最近波動幅度={vol:.2f}, 自動 lookback={lb}")
                # 檢查是否出現背離
                alert = check_divergence(df_complete)
                
                # 如果出現新背離、且超過冷卻時間，就發 Telegram 通知
                now = datetime.now()
                if alert and alert != last_alert and now - last_alert_time > cooldown:
                    msg = f"⚠️ {alert}\n⏰ {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n💰 {price}"
                    send_alert(msg)
                    last_alert = alert
                    last_alert_time = now
                    print(f"\n🔔 發送警報: {alert}\n")
        # 每 3 秒更新一次行情
        time.sleep(3)


app = Flask(__name__)

# Render 健康檢查會 ping "/"，你必須回應 200 才會被認為 OK
@app.route("/")
def home():
    return "Service is running", 200

def run_bot():
    main()   # ← 你的原本邏輯

if __name__ == "__main__":
    # 把你的主程式放進 Thread（不阻塞 Flask）
    t = threading.Thread(target=run_bot)
    t.daemon = True
    t.start()
    
    # 啟動 self-ping thread，防止 Render 休眠
    t2 = threading.Thread(target=keep_alive, args=("https://macd-rx43.onrender.com",))
    t2.daemon = True
    t2.start()

    # Flask 必須綁定 0.0.0.0 才能在 Render 運行
    app.run(host="0.0.0.0", port=10000)