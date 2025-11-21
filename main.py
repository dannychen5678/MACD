import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta
from flask import Flask
import threading

# === Telegram 設定 ===
BOT_TOKEN = "8262097219:AAGEtNSYY81GrtupVILIxqTA2rnt7Z0woUo"
CHAT_ID = "8414393276"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# === 台指期即時行情 URL ===
URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"

def get_market_type():
    """切換交易時段"""
    now = datetime.now().time()
    if datetime.strptime("08:45", "%H:%M").time() <= now <= datetime.strptime("13:45", "%H:%M").time():
        return "0"
    if now >= datetime.strptime("15:00", "%H:%M").time() or now <= datetime.strptime("05:00", "%H:%M").time():
        return "1"
    return "0"

def get_payload():  
    return {
        "MarketType": get_market_type(),
        "SymbolType": "F",
        "KindID": "1",
        "CID": "TXF",
        "ExpireMonth": "",      
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A"
    }

def keep_alive(url):
    """自我保持運作"""
    while True:
        try:
            requests.get(url)
            print("Pinged self to stay awake")
        except:
            pass
        time.sleep(600)

def send_alert(msg):
    """發送通知給 Telegram"""
    requests.post(API_URL, data={"chat_id": CHAT_ID, "text": msg})

def fetch_latest_price():
    """抓取最新成交價"""
    try:
        r = requests.post(URL, json=get_payload(), headers={"Content-Type": "application/json"})
        data = r.json()
        quotes = data.get("RtData", {}).get("QuoteList", [])
        
        if not quotes:
            print("⚠️ 沒有取得 QuoteList")
            return None, None, None

        txf_list = [q for q in quotes if q["SymbolID"].startswith("TXF") and q["CLastPrice"]]
        if not txf_list:
            print("⚠️ 找不到近月台指期報價")
            return None, None, None

        q = txf_list[0]
        price = float(q["CLastPrice"])
        ref_price = float(q["CRefPrice"]) if q["CRefPrice"] else price
        timestamp = datetime.now()
        
        return timestamp, price, ref_price

    except Exception as e:
        print("❌ 抓取成交價失敗:", e)
        return None, None, None

# === Impulse MACD 計算（與第一段程式碼相同） ===
def _smma(series, period):
    """計算平滑移動平均線（與第一段程式碼相同）"""
    smma_output = pd.Series(np.nan, index=series.index)
    sma_val = series.rolling(window=period).mean()
    first_valid_index = sma_val.first_valid_index()
    
    if first_valid_index is None:
        return smma_output
    
    try:
        start_loc = series.index.get_loc(first_valid_index)
    except KeyError:
        return smma_output
    
    smma_output.loc[first_valid_index] = sma_val.loc[first_valid_index]
    
    for i in range(start_loc + 1, len(series)):
        prev_smma = smma_output.iloc[i - 1]
        current_val = series.iloc[i]
        if pd.notna(prev_smma) and pd.notna(current_val):
            smma_output.iloc[i] = (prev_smma * (period - 1) + current_val) / period
        else:
            smma_output.iloc[i] = np.nan
    
    return smma_output

def calc_impulse_macd(df, ma_len=30, sig_len=8):
    """計算 Impulse MACD（與第一段程式碼完全相同的邏輯）"""
    # 計算 hlc3
    df['hlc3'] = (df['high'] + df['low'] + df['close']) / 3
    
    # 計算 SMMA
    df['High_smma'] = _smma(df['high'], period=ma_len)
    df['Low_smma'] = _smma(df['low'], period=ma_len)
    
    # 計算 DEMA（雙重指數移動平均）
    ema1 = df['hlc3'].ewm(span=ma_len, adjust=False).mean()
    df['hlc3_zlema'] = ema1.ewm(span=ma_len, adjust=False).mean()
    
    # 計算 md（動能差）
    df['md'] = np.where(
        df['hlc3_zlema'] > df['High_smma'], 
        df['hlc3_zlema'] - df['High_smma'],
        np.where(df['hlc3_zlema'] < df['Low_smma'], 
                 df['hlc3_zlema'] - df['Low_smma'], 
                 0)
    )
    
    # 計算訊號線
    df['sb'] = df['md'].rolling(window=sig_len).mean()
    
    return df

def check_impulse_signal(df):
    """檢查 md 與 sb 的穿越訊號（與第一段程式碼相同）"""
    if len(df) < 2 or 'md' not in df.columns or 'sb' not in df.columns:
        return None
    
    # 確保有足夠資料
    if pd.isna(df['md'].iloc[-1]) or pd.isna(df['sb'].iloc[-1]):
        return None
    if pd.isna(df['md'].iloc[-2]) or pd.isna(df['sb'].iloc[-2]):
        return None
    
    md_prev = df['md'].iloc[-2]
    sb_prev = df['sb'].iloc[-2]
    md_now = df['md'].iloc[-1]
    sb_now = df['sb'].iloc[-1]
    
    # 黃金交叉：md 向上穿越 sb → 看多
    if md_prev < sb_prev and md_now > sb_now:
        return "看多訊號（md 向上穿越 sb）"
    
    # 死亡交叉：md 向下穿越 sb → 看空
    if md_prev > sb_prev and md_now < sb_now:
        return "看空訊號（md 向下穿越 sb）"
    
    return None

# === 主程式 ===
def main():
    print("=" * 60)
    print("🔍 開始監控台指期 Impulse MACD 訊號")
    print("=" * 60)
    print("📌 指標系統：Impulse MACD (與專家程式碼相同)")
    print("📌 參數設定：ma_len=30, sig_len=8")
    print("📌 資料頻率：5 分鐘 K 線")
    print("📌 資料保留：48 小時（確保指標穩定）")
    print("📌 最少需求：80 根 K 線（約 6.5 小時交易時間）")
    print("📌 更新頻率：每 3 秒檢查一次，只記錄價格變動")
    print("=" * 60)
    print("\n💡 建議啟動時間：")
    print("   - 日盤交易者：08:30 啟動 → 15:00 開始監控")
    print("   - 夜盤交易者：08:30 啟動 → 15:00 開始監控")
    print("   - 當天就能在夜盤使用，隔天日盤也能用")
    print("=" * 60 + "\n")
    
    df_tick = pd.DataFrame(columns=['Close'])
    last_alert = None
    last_alert_time = datetime.min
    cooldown = timedelta(minutes=5)
    ref_price = None
    data_ready = False
    last_price = None  # 記錄上一次的價格
    last_record_time = None  # 記錄上一次記錄的時間
    
    while True:
        timestamp, price, current_ref = fetch_latest_price()
        
        if price:
            if current_ref and not ref_price:
                ref_price = current_ref
            
            # 只在以下情況記錄價格：
            # 1. 價格改變了（避免重複記錄相同價格）
            # 2. 或者距離上次記錄超過 30 秒（避免長時間沒成交導致資料斷層）
            should_record = False
            
            if last_price is None or price != last_price:
                should_record = True  # 價格改變，記錄
            elif last_record_time is None or (timestamp - last_record_time).total_seconds() >= 30:
                should_record = True  # 超過 30 秒沒記錄，記錄一次
            
            if should_record:
                # 確保 index 是時間格式
                df_tick.index = pd.to_datetime(df_tick.index, errors='coerce')

                # 保留最近 48 小時資料
                cutoff_time = datetime.now() - timedelta(hours=48)
                df_tick = df_tick.loc[df_tick.index >= cutoff_time]

                # 記錄價格
                df_tick.loc[timestamp] = price
                last_price = price
                last_record_time = timestamp
            
            # 重新整理成「5 分鐘 K 線」（使用 '5min' 取代已棄用的 '5T'）
            df_5min = df_tick['Close'].resample('5min').ohlc()
            df_5min['volume'] = df_tick['Close'].resample('5min').count()
            df_5min.dropna(inplace=True)
            
            # 計算資料涵蓋的時間範圍
            if len(df_5min) > 0:
                data_hours = (df_5min.index[-1] - df_5min.index[0]).total_seconds() / 3600
            else:
                data_hours = 0
            
            # 顯示目前狀況（每次都顯示，但只在價格變動時記錄）
            record_status = "✅ 已記錄" if should_record else "⏸️ 未變動"
            print(f"📈 {timestamp.strftime('%H:%M:%S')} | 價格: {price:,.0f} {record_status} | "
                  f"Tick: {len(df_tick)} | K線: {len(df_5min)}根 | "
                  f"涵蓋: {data_hours:.1f}小時")
            
            # 資料量需求說明：
            # - SMMA(30) 需要至少 60 根才穩定
            # - DEMA(30) 需要至少 60 根才穩定  
            # - SMA(8) 需要至少 16 根才穩定
            # - 總計：至少需要 80 根 K 線
            
            if len(df_5min) < 80:
                if not data_ready:
                    remaining = 80 - len(df_5min)
                    eta_minutes = remaining * 5
                    eta_hours = eta_minutes / 60
                    
                    # 計算預計完成時間
                    estimated_ready_time = datetime.now() + timedelta(minutes=eta_minutes)
                    
                    print(f"⏳ 資料累積中... 還需要 {remaining} 根 K 線")
                    print(f"   預計時間：約 {eta_hours:.1f} 小時（{eta_minutes} 分鐘）")
                    print(f"   預計完成：{estimated_ready_time.strftime('%H:%M')} 左右")
                continue
            
            # 第一次達到足夠資料時顯示訊息
            if not data_ready:
                data_ready = True
                print("\n" + "=" * 60)
                print("✅ 資料量已足夠，開始監控訊號！")
                print("=" * 60 + "\n")
            
            # 使用正確的 Impulse MACD 計算
            df_5min = calc_impulse_macd(df_5min, ma_len=30, sig_len=8)
            
            # 檢查最新的 md 和 sb 值
            if not pd.isna(df_5min['md'].iloc[-1]) and not pd.isna(df_5min['sb'].iloc[-1]):
                md_val = df_5min['md'].iloc[-1]
                sb_val = df_5min['sb'].iloc[-1]
                
                # 計算 md 與 sb 的距離（判斷是否接近穿越）
                distance = md_val - sb_val
                
                # 顯示指標狀態
                if abs(distance) < 10:
                    status = "🔥 接近穿越"
                elif distance > 0:
                    status = "📈 多頭區"
                else:
                    status = "📉 空頭區"
                
                print(f"📊 md={md_val:+.2f}, sb={sb_val:+.2f}, 差距={distance:+.2f} | {status}")
            
            # 檢查穿越訊號
            alert = check_impulse_signal(df_5min)
            
            # 如果出現新訊號、且超過冷卻時間，就發 Telegram 通知
            now = datetime.now()
            if alert and alert != last_alert and now - last_alert_time > cooldown:
                msg = (f"⚠️ {alert}\n"
                       f"⏰ {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
                       f"💰 價格: {price:,.0f}\n"
                       f"📊 md={md_val:+.2f}, sb={sb_val:+.2f}")
                send_alert(msg)
                last_alert = alert
                last_alert_time = now
                print("\n" + "🔔" * 30)
                print(f"🔔 發送警報: {alert}")
                print("🔔" * 30 + "\n")
        
        # 每 3 秒更新一次行情
        time.sleep(3)


app = Flask(__name__)

@app.route("/")
def home():
    return "Service is running", 200

def run_bot():
    main()

if __name__ == "__main__":
    t = threading.Thread(target=run_bot)
    t.daemon = True
    t.start()
    
    t2 = threading.Thread(target=keep_alive, args=("https://macd-rx43.onrender.com",))
    t2.daemon = True
    t2.start()

    app.run(host="0.0.0.0", port=10000)
