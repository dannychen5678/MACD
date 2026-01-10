import requests
import pandas as pd
import numpy as np
import time
import json
import os
from datetime import datetime, timedelta
from flask import Flask
import threading
from pathlib import Path
from sqlalchemy import create_engine, Column, Integer, Float, String, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import pytz

# 設定台灣時區
TW_TZ = pytz.timezone('Asia/Taipei')

# === Telegram 設定 ===
BOT_TOKEN = "8262097219:AAGEtNSYY81GrtupVILIxqTA2rnt7Z0woUo"
CHAT_ID = "8414393276"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# === 台指期即時行情 URL ===
URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"

# === 資料庫設定 ===
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///macd_data.db')
# Render 的 PostgreSQL URL 格式修正
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

Base = declarative_base()

# 資料庫模型
class SignalLog(Base):
    __tablename__ = 'signal_logs'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False)
    signal_type = Column(String(100), nullable=False)
    entry_price = Column(Float, nullable=False)
    slope = Column(Float)
    hist_avg = Column(Float)
    hist_now = Column(Float)
    price_range = Column(Float)
    slope_threshold = Column(Float)
    lookback = Column(Integer)
    price_10min = Column(Float)
    price_30min = Column(Float)
    price_1hour = Column(Float)
    result = Column(String(20))
    profit_loss = Column(Float)
    threshold_used = Column(Float)

class Parameters(Base):
    __tablename__ = 'parameters'
    
    id = Column(Integer, primary_key=True)
    slope_threshold = Column(Float, nullable=False)
    lookback = Column(Integer, nullable=False)
    hist_confirm_bars = Column(Integer, nullable=False)
    cooldown_minutes = Column(Integer, nullable=False)
    last_update = Column(DateTime, nullable=False)

# 建立資料庫連線
engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# 備用本地儲存（如果資料庫連線失敗）
DATA_DIR = Path("macd_data")
DATA_DIR.mkdir(exist_ok=True)
PARAMS_FILE = DATA_DIR / "parameters.json"

class SessionMonitor:
    """
    交易時段監控器 - 以開盤價為基準點計算漲跌幅
    日盤：08:45 開盤，以開盤價為基準
    夜盤：15:00 開盤，以開盤價為基準
    """
    def __init__(self):
        self.session_open_price = None  # 開盤價（基準點）
        self.session_open_time = None
        self.session_type = None  # "日盤" 或 "夜盤"
        self.notified_levels = set()
        self.alert_interval = 100   # 每漲/跌 100 點通知
        self.min_alert_change = 500   # 漲/跌 500 點才開始通知
        self.is_session_started = False

    def update(self, df_5min):
        if len(df_5min) < 2:
            return None, None

        current_price = float(df_5min['close'].iloc[-1])
        current_time = df_5min.index[-1]
        
        # 檢查是否為開盤時間
        current_hour = get_taiwan_time().hour
        current_minute = get_taiwan_time().minute
        
        is_day_open = (current_hour == 8 and 45 <= current_minute <= 50)
        is_night_open = (current_hour == 15 and 0 <= current_minute <= 5)
        
        # 開盤時設定基準點為開盤價
        if (is_day_open or is_night_open) and not self.is_session_started:
            self.session_open_price = current_price
            self.session_open_time = current_time
            self.session_type = "日盤" if is_day_open else "夜盤"
            self.notified_levels.clear()
            self.is_session_started = True
            
            send_alert(
                f"✅ {self.session_type}開盤\n"
                f"📊 開盤價: {current_price:,.0f}\n"
                f"🎯 開始監控從開盤價算起的漲跌幅"
            )
            return None, None

        # 如果還沒開始交易時段，不進行監控
        if not self.is_session_started or self.session_open_price is None:
            return None, None

        # 計算從開盤價開始的變化
        change = current_price - self.session_open_price
        
        # 判斷是上漲還是下跌
        if abs(change) < self.min_alert_change:
            return None, None
        
        # 計算級距
        current_level = int((abs(change) // self.alert_interval) * self.alert_interval)
        
        # 建立通知標識（區分上漲和下跌）
        if change > 0:
            level_key = f"up_{current_level}"
            signal_type = f"從開盤價上漲 {current_level} 點"
        else:
            level_key = f"down_{current_level}"
            signal_type = f"從開盤價下跌 {current_level} 點"

        if level_key not in self.notified_levels:
            self.notified_levels.add(level_key)

            signal_data = {
                'session_open_price': self.session_open_price,
                'current_price': current_price,
                'change': change,
                'change_level': current_level,
                'open_time': self.session_open_time,
                'session_type': self.session_type
            }

            return signal_type, signal_data

        return None, None
    
    def reset(self):
        """重置監控器"""
        print("🔄 交易時段監控器重置")
        self.session_open_price = None
        self.session_open_time = None
        self.session_type = None
        self.notified_levels.clear()
        self.is_session_started = False

# === 動態參數（會自動調整） ===
class DynamicParams:
    def __init__(self):
        self.slope_threshold = 3.0
        self.lookback = 10
        self.hist_confirm_bars = 3
        self.cooldown_minutes = 5
        self.min_signals_for_optimization = 20
        self.load_params()
    
    def load_params(self):
        """載入已儲存的參數（從資料庫）"""
        try:
            session = Session()
            param = session.query(Parameters).order_by(Parameters.last_update.desc()).first()
            if param:
                self.slope_threshold = param.slope_threshold
                self.lookback = param.lookback
                self.hist_confirm_bars = param.hist_confirm_bars
                self.cooldown_minutes = param.cooldown_minutes
                print(f"✅ 從資料庫載入參數: slope={self.slope_threshold}, lookback={self.lookback}")
            session.close()
        except Exception as e:
            print(f"⚠️ 資料庫載入失敗，使用預設參數: {e}")
            # 備用：從本地檔案載入
            if PARAMS_FILE.exists():
                with open(PARAMS_FILE, 'r') as f:
                    params = json.load(f)
                    self.slope_threshold = params.get('slope_threshold', 3.0)
                    self.lookback = params.get('lookback', 10)
    
    def save_params(self):
        """儲存參數（到資料庫）"""
        try:
            session = Session()
            param = Parameters(
                slope_threshold=self.slope_threshold,
                lookback=self.lookback,
                hist_confirm_bars=self.hist_confirm_bars,
                cooldown_minutes=self.cooldown_minutes,
                last_update=datetime.now()
            )
            session.add(param)
            session.commit()
            session.close()
            print(f"✅ 參數已儲存到資料庫")
        except Exception as e:
            print(f"⚠️ 資料庫儲存失敗: {e}")
            # 備用：儲存到本地檔案
            params = {
                'slope_threshold': self.slope_threshold,
                'lookback': self.lookback,
                'hist_confirm_bars': self.hist_confirm_bars,
                'cooldown_minutes': self.cooldown_minutes,
                'last_update': datetime.now().isoformat()
            }
            with open(PARAMS_FILE, 'w') as f:
                json.dump(params, f, indent=2)

params = DynamicParams()

def get_taiwan_time():
    """取得台灣時間"""
    return datetime.now(TW_TZ)

def get_market_type():
    """切換交易時段"""
    now = get_taiwan_time().time()
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
    """自我保持運作（每 5 分鐘 ping 一次）"""
    while True:
        try:
            response = requests.get(url, timeout=10)
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if response.status_code == 200:
                print(f"✅ [{current_time}] Keep-alive 成功 (Status: {response.status_code})")
            else:
                print(f"⚠️ [{current_time}] Keep-alive 異常 (Status: {response.status_code})")
        except Exception as e:
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"❌ [{current_time}] Keep-alive 失敗: {e}")
        
        # 每 5 分鐘 ping 一次（Render 免費方案 15 分鐘沒請求會休眠）
        time.sleep(300)

def send_alert(msg):
    """發送通知給 Telegram"""
    requests.post(API_URL, data={"chat_id": CHAT_ID, "text": msg})

def fetch_latest_price():
    """抓取最新成交價"""
    try:
        r = requests.post(URL, json=get_payload(), headers={"Content-Type": "application/json"})
        
        if r.status_code != 200:
            return None, None, None
        
        data = r.json()
        quotes = data.get("RtData", {}).get("QuoteList", [])
        
        if not quotes:
            return None, None, None

        # 優先選擇近月合約（通常是 TXFL5-M 或類似格式）
        # 過濾條件：
        # 1. SymbolID 包含 "TXF" 但不是 "TXF-P"（現貨）
        # 2. 有成交價 CLastPrice
        # 3. 有成交量 CTotalVolume
        txf_list = [
            q for q in quotes 
            if "TXF" in q["SymbolID"] 
            and q["SymbolID"] != "TXF-P"  # 排除現貨
            and q.get("CLastPrice") 
            and q.get("CLastPrice") != ""
            and q.get("CTotalVolume")
            and q.get("CTotalVolume") != ""
        ]
        
        if not txf_list:
            return None, None, None

        # 選擇成交量最大的合約（通常是近月）
        q = max(txf_list, key=lambda x: int(x.get("CTotalVolume", "0") or "0"))
        
        price = float(q["CLastPrice"])
        ref_price = float(q["CRefPrice"]) if q["CRefPrice"] else price
        timestamp = get_taiwan_time()  # 使用台灣時間
        
        # 顯示選擇的合約（前 3 次）
        global fetch_count
        if 'fetch_count' not in globals():
            fetch_count = 0
        fetch_count += 1
        if fetch_count <= 3:
            print(f"📊 選擇合約: {q['SymbolID']} | 價格: {price:,.0f} | 成交量: {q['CTotalVolume']}")
        
        return timestamp, price, ref_price

    except Exception as e:
        print(f"❌ 抓取價格失敗: {e}")
        return None, None, None

# === 標準 MACD 計算 ===
def calc_macd(df):
    """計算標準 MACD (12, 26, 9)"""
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['Histogram'] = df['MACD'] - df['Signal']
    return df

# 建立全域監控器
session_monitor = SessionMonitor()

# === 階段 1：數據收集 ===
def record_signal(signal_type, price, signal_data, df_5min):
    """記錄訊號到資料庫"""
    try:
        session = Session()
        
        # 根據訊號類型調整資料欄位
        if '下跌' in signal_type:
            slope = float(signal_data.get('change', 0))
            hist_avg = float(signal_data.get('session_open_price', price))
            hist_now = float(signal_data.get('current_price', price))
            price_range = abs(float(signal_data.get('change', 0)))
        elif '上漲' in signal_type:
            slope = float(signal_data.get('change', 0))
            hist_avg = float(signal_data.get('session_open_price', price))
            hist_now = float(signal_data.get('current_price', price))
            price_range = abs(float(signal_data.get('change', 0)))
        else:
            slope = 0
            hist_avg = price
            hist_now = price
            price_range = 0
        
        signal = SignalLog(
            timestamp=datetime.now(),
            signal_type=signal_type,
            entry_price=price,
            slope=slope,
            hist_avg=hist_avg,
            hist_now=hist_now,
            price_range=price_range,
            slope_threshold=params.slope_threshold,
            lookback=params.lookback
        )
        session.add(signal)
        session.commit()
        session.close()
        print(f"✅ 訊號已記錄到資料庫: {signal_type}")
        
    except Exception as e:
        print(f"❌ 記錄訊號失敗: {e}")

def update_signal_results(df_5min):
    """更新訊號結果（追蹤價格變化）"""
    try:
        session = Session()
        current_time = datetime.now()
        current_price = float(df_5min['close'].iloc[-1])
        
        # 查詢所有未完成的訊號
        pending_signals = session.query(SignalLog).filter(SignalLog.result == None).all()
        
        for signal in pending_signals:
            time_diff = (current_time - signal.timestamp).total_seconds() / 60
            
            # 更新 10 分鐘後價格
            if signal.price_10min is None and time_diff >= 10:
                signal.price_10min = current_price
            
            # 更新 30 分鐘後價格
            if signal.price_30min is None and time_diff >= 30:
                signal.price_30min = current_price
            
            # 更新 1 小時後價格並判斷結果
            if signal.price_1hour is None and time_diff >= 60:
                signal.price_1hour = current_price
                
                # 判斷訊號結果
                if '上漲' in signal.signal_type:
                    profit_loss = current_price - signal.entry_price
                else:  # 下跌
                    profit_loss = signal.entry_price - current_price
                
                signal.profit_loss = profit_loss
                
                # 動態門檻
                dynamic_threshold = max(20, min(50, signal.price_range * 0.3))
                
                # 判斷成功或失敗
                if profit_loss > dynamic_threshold:
                    signal.result = 'success'
                elif profit_loss < -dynamic_threshold:
                    signal.result = 'fail'
                else:
                    signal.result = 'neutral'
                
                signal.threshold_used = dynamic_threshold
                print(f"✅ 訊號結果已更新: {signal.signal_type} -> {signal.result}")
        
        session.commit()
        session.close()
        
    except Exception as e:
        print(f"❌ 更新訊號結果失敗: {e}")

# === 階段 2：結果分析 ===
def analyze_signals():
    """分析訊號勝率（從資料庫）"""
    try:
        session = Session()
        
        # 查詢所有已完成的訊號
        completed_signals = session.query(SignalLog).filter(SignalLog.result != None).all()
        
        if len(completed_signals) == 0:
            session.close()
            return None
        
        # 轉換為 DataFrame 方便分析
        data = [{
            'signal_type': s.signal_type,
            'result': s.result,
            'profit_loss': s.profit_loss
        } for s in completed_signals]
        df_completed = pd.DataFrame(data)
        
        stats = {
            'total_signals': len(df_completed),
            'success_count': len(df_completed[df_completed['result'] == 'success']),
            'fail_count': len(df_completed[df_completed['result'] == 'fail']),
            'neutral_count': len(df_completed[df_completed['result'] == 'neutral']),
            'success_rate': 0,
            'avg_profit': df_completed['profit_loss'].mean(),
            'by_signal_type': {}
        }
        
        stats['success_rate'] = stats['success_count'] / len(df_completed) * 100
        
        # 分析各種訊號類型
        for signal_type in df_completed['signal_type'].unique():
            df_type = df_completed[df_completed['signal_type'] == signal_type]
            success = len(df_type[df_type['result'] == 'success'])
            total = len(df_type)
            
            stats['by_signal_type'][signal_type] = {
                'total': total,
                'success': success,
                'success_rate': success / total * 100 if total > 0 else 0,
                'avg_profit': df_type['profit_loss'].mean()
            }
        
        session.close()
        return stats
        
    except Exception as e:
        print(f"❌ 分析訊號失敗: {e}")
        return None

def print_statistics(stats):
    """打印統計報告"""
    if not stats:
        return
    
    print("\n" + "=" * 80)
    print("📊 訊號統計報告")
    print("=" * 80)
    print(f"總訊號數: {stats['total_signals']}")
    print(f"成功: {stats['success_count']} | 失敗: {stats['fail_count']} | 中性: {stats['neutral_count']}")
    print(f"整體勝率: {stats['success_rate']:.1f}%")
    print(f"平均損益: {stats['avg_profit']:+.1f} 點")
    
    print("\n各類訊號表現:")
    for signal_type, data in stats['by_signal_type'].items():
        print(f"  {signal_type}:")
        print(f"    數量: {data['total']} | 勝率: {data['success_rate']:.1f}% | 平均損益: {data['avg_profit']:+.1f} 點")
    
    print("=" * 80 + "\n")

# === 階段 3：自動調整參數 ===
def optimize_parameters(stats):
    """根據勝率自動調整參數"""
    if not stats or stats['total_signals'] < params.min_signals_for_optimization:
        print(f"⏳ 訊號數量不足，需要至少 {params.min_signals_for_optimization} 個訊號才能優化")
        return False
    
    success_rate = stats['success_rate']
    old_slope = params.slope_threshold
    old_lookback = params.lookback
    
    print("\n" + "=" * 80)
    print("🤖 開始自動優化參數")
    print("=" * 80)
    print(f"當前勝率: {success_rate:.1f}%")
    print(f"當前參數: slope_threshold={old_slope}, lookback={old_lookback}")
    
    # 優化邏輯
    if success_rate < 55:
        # 勝率太低，提高門檻減少假訊號
        params.slope_threshold = min(old_slope + 0.5, 6.0)
        params.lookback = min(old_lookback + 2, 15)
        print("📉 勝率偏低，提高門檻以減少假訊號")
        
    elif success_rate > 75:
        # 勝率很高，降低門檻增加訊號數量
        params.slope_threshold = max(old_slope - 0.5, 2.0)
        params.lookback = max(old_lookback - 1, 8)
        print("📈 勝率良好，降低門檻以增加訊號")
        
    elif 60 <= success_rate <= 70:
        # 勝率適中，微調參數
        avg_profit = stats['avg_profit']
        if avg_profit < 20:
            params.slope_threshold = old_slope + 0.2
            print("💰 平均獲利偏低，微調門檻")
    
    # 儲存新參數
    params.save_params()
    
    print(f"新參數: slope_threshold={params.slope_threshold}, lookback={params.lookback}")
    print("=" * 80 + "\n")
    
    # 發送通知
    msg = (f"🤖 參數已自動優化\n"
           f"勝率: {success_rate:.1f}%\n"
           f"slope: {old_slope} → {params.slope_threshold}\n"
           f"lookback: {old_lookback} → {params.lookback}")
    send_alert(msg)
    
    return True

# === 主程式 ===
def main():
    import sys
    print("=" * 60, flush=True)
    print("🤖 開始監控台指期價格（以開盤價為基準）", flush=True)
    print("=" * 60, flush=True)
    print("📌 監控策略：以開盤價為基準點計算漲跌幅", flush=True)
    print("📌 日盤開盤：08:45 | 夜盤開盤：15:00", flush=True)
    print("📌 通知規則：漲/跌 500 點後開始通知，之後每 100 點通知一次", flush=True)
    print("📌 週一重啟：每週一 08:30 清空週末資料", flush=True)
    print("=" * 60 + "\n", flush=True)
    sys.stdout.flush()
    
    df_tick = pd.DataFrame(columns=['Close'])
    df_tick.index = pd.DatetimeIndex([])  # 初始化為空的 DatetimeIndex
    last_alert = None
    last_alert_time = datetime.min.replace(tzinfo=TW_TZ)
    last_price = None
    last_record_time = None
    data_ready = False
    last_analysis_time = get_taiwan_time()
    last_heartbeat = get_taiwan_time()  # 心跳計時器
    loop_count = 0  # 循環計數器
    last_reset_date = get_taiwan_time().date()  # 記錄上次重置日期
    last_keepalive_check = get_taiwan_time()  # Keep-alive 檢查計時器
    
    while True:
        loop_count += 1
        
        # === 檢查是否需要週一重置 ===
        current_time = get_taiwan_time()  # 使用台灣時間
        current_date = current_time.date()
        current_hour = current_time.hour
        current_minute = current_time.minute
        
        # 每週一 08:30 重置一次（週末後重新開始）
        if (current_date.weekday() == 0 and  # 週一
            current_date != last_reset_date and 
            current_hour == 8 and 
            30 <= current_minute < 35):  # 08:30-08:35 之間執行
            
            print("\n" + "=" * 70)
            print("🌅 週一開盤前自動重置（週末後重新開始）")
            print("=" * 70)
            print(f"⏰ 重置時間: {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print("🔄 清空週末資料，重新開始監控")
            print("=" * 70 + "\n")
            
            # 重置監控器
            session_monitor.reset()
            
            # 清空 tick 資料（保持 DatetimeIndex）
            df_tick = pd.DataFrame(columns=['Close'])
            df_tick.index = pd.DatetimeIndex([])  # 設定為空的 DatetimeIndex
            last_alert = None
            last_alert_time = get_taiwan_time() - timedelta(days=3650)
            data_ready = False
            last_reset_date = current_date
            
            # 發送重置通知
            msg = (f"🌅 週一開盤前自動重置\n"
                   f"⏰ {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"🔄 清空週末資料\n"
                   f"🎯 等待開盤設定基準點")
            send_alert(msg)
            
            print("✅ 重置完成，繼續監控...\n")
        
        # 每 60 秒顯示一次心跳訊息（無論是否有價格）
        now_tw = get_taiwan_time()
        if (now_tw - last_heartbeat).total_seconds() >= 60:
            import sys
            session_status = f"{session_monitor.session_type}監控中" if session_monitor.is_session_started else "等待開盤"
            print(f"💓 心跳 #{loop_count} | {now_tw.strftime('%Y-%m-%d %H:%M:%S')} (台灣) | {session_status}...", flush=True)
            sys.stdout.flush()
            last_heartbeat = now_tw
        
        # 每 4 分鐘檢查一次 Keep-alive 執行緒是否正常（備用機制）
        if (now_tw - last_keepalive_check).total_seconds() >= 240:
            # 檢查 Keep-alive 執行緒是否還活著
            keepalive_alive = any(t.name == "KeepAliveThread" and t.is_alive() for t in threading.enumerate())
            if not keepalive_alive:
                print("⚠️ Keep-alive 執行緒已停止，嘗試重啟...", flush=True)
            last_keepalive_check = now_tw
        
        timestamp, price, current_ref = fetch_latest_price()
        
        # 如果沒有價格，顯示警告（前 5 次）
        if not price and loop_count <= 5:
            import sys
            print(f"⚠️ [{loop_count}] 無法取得價格 | {datetime.now().strftime('%H:%M:%S')} | 可能是休市時間", flush=True)
            sys.stdout.flush()
        
        if price:
            # 每次成功抓取價格時顯示（前 10 次）
            if loop_count <= 10:
                import sys
                print(f"📊 [{loop_count}] 抓取價格: {price:,.0f} | {timestamp.strftime('%H:%M:%S')}", flush=True)
                sys.stdout.flush()
            should_record = False
            
            if last_price is None or price != last_price:
                should_record = True
            elif last_record_time is None or (timestamp - last_record_time).total_seconds() >= 30:
                should_record = True
            
            if should_record:
                # 確保索引是 DatetimeIndex
                if not isinstance(df_tick.index, pd.DatetimeIndex):
                    df_tick.index = pd.DatetimeIndex([])
                
                # 清理超過 48 小時的舊資料
                cutoff_time = get_taiwan_time() - timedelta(hours=48)
                if len(df_tick) > 0:
                    df_tick = df_tick.loc[df_tick.index >= cutoff_time]
                
                # 記錄新價格
                df_tick.loc[timestamp] = price
                last_price = price
                last_record_time = timestamp
            
            df_5min = df_tick['Close'].resample('5min').ohlc()
            df_5min['volume'] = df_tick['Close'].resample('5min').count()
            df_5min.dropna(inplace=True)
            
            # 至少需要 2 根 K 棒才能開始處理
            if len(df_5min) < 2:
                continue
            
            if not data_ready and len(df_5min) >= 2:
                data_ready = True
                print("\n" + "=" * 60)
                print("✅ 開始收集資料")
                print("=" * 60)
                print(f"📊 當前有 {len(df_5min)} 根 5 分鐘 K 棒")
                print(f"📈 最新價格: {price:,.0f}")
                print(f"🎯 等待開盤時設定基準點")
                print("=" * 60 + "\n")
            
            df_5min = calc_macd(df_5min)
            
            # 更新訊號結果
            update_signal_results(df_5min)
            
            # 每 30 分鐘分析一次並優化參數
            if (get_taiwan_time() - last_analysis_time).total_seconds() >= 1800:
                stats = analyze_signals()
                if stats:
                    print_statistics(stats)
                    optimize_parameters(stats)
                last_analysis_time = get_taiwan_time()
            
            # 檢查價格變化訊號
            alert, signal_data = session_monitor.update(df_5min)
            
            # 每 3 分鐘顯示一次詳細狀態
            if data_ready and loop_count % 60 == 0:  # 每 60 個循環（約 3 分鐘）
                if session_monitor.is_session_started:
                    open_price = session_monitor.session_open_price
                    change = price - open_price
                    session_type = session_monitor.session_type
                    print(f"📊 {datetime.now().strftime('%H:%M:%S')} | "
                          f"{session_type} | "
                          f"開盤價: {open_price:,.0f} | "
                          f"現價: {price:,.0f} | "
                          f"變化: {change:+.0f} | "
                          f"K棒: {len(df_5min)} | "
                          f"循環: #{loop_count}")
                else:
                    print(f"⏳ {get_taiwan_time().strftime('%H:%M:%S')} | "
                          f"等待開盤 | "
                          f"現價: {price:,.0f} | "
                          f"K棒: {len(df_5min)} | "
                          f"循環: #{loop_count}")
            
            now = get_taiwan_time()
            cooldown = timedelta(minutes=params.cooldown_minutes)

            if alert and alert != last_alert and now - last_alert_time > cooldown:
                # 記錄訊號
                record_signal(alert, price, signal_data, df_5min)
                
                # 發送通知
                open_time_str = signal_data['open_time'].strftime('%H:%M:%S')
                session_type = signal_data['session_type']
                
                if '上漲' in alert:
                    msg = (f"🚀 {alert}\n"
                           f"⏰ {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
                           f"📊 {session_type}開盤價: {signal_data['session_open_price']:,.0f} ({open_time_str})\n"
                           f"📈 現價: {signal_data['current_price']:,.0f}\n"
                           f"💥 漲幅: {signal_data['change']:.0f} 點\n"
                           f"🎯 級距: {signal_data['change_level']} 點")
                else:  # 下跌
                    msg = (f"⚠️ {alert}\n"
                           f"⏰ {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
                           f"📊 {session_type}開盤價: {signal_data['session_open_price']:,.0f} ({open_time_str})\n"
                           f"📉 現價: {signal_data['current_price']:,.0f}\n"
                           f"💥 跌幅: {abs(signal_data['change']):.0f} 點\n"
                           f"🎯 級距: {signal_data['change_level']} 點")
                
                send_alert(msg)
                
                last_alert = alert
                last_alert_time = now
                print(f"\n🔔 發送警報: {alert}\n")
        
        time.sleep(3)


app = Flask(__name__)

@app.route("/")
def home():
    return "Service is running (Open Price Based Version)", 200

@app.route("/health")
def health():
    """健康檢查端點 - 快速回應"""
    return {"status": "ok", "service": "session-monitor", "timestamp": datetime.now().isoformat()}, 200

@app.route("/heartbeat")
def heartbeat():
    """心跳檢查 - 確認服務持續運行"""
    current_time = datetime.now()
    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>心跳監控</title>
        <meta http-equiv="refresh" content="10">
        <style>
            body {{ font-family: monospace; background: #1e1e1e; color: #00ff00; padding: 20px; }}
            .pulse {{ animation: pulse 1s infinite; }}
            @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
            .time {{ font-size: 2em; margin: 20px 0; }}
        </style>
    </head>
    <body>
        <h1><span class="pulse">💚</span> 系統心跳監控</h1>
        <div class="time">⏰ {current_time.strftime('%Y-%m-%d %H:%M:%S')}</div>
        <p>✅ 服務正常運行</p>
        <p>🔄 每 10 秒自動刷新</p>
        <p>💡 如果時間停止更新，表示服務已關閉</p>
        <hr>
        <p><a href="/" style="color: #00ff00;">返回首頁</a></p>
    </body>
    </html>
    """, 200

@app.route("/signals")
def view_signals():
    """查看所有訊號記錄"""
    try:
        session = Session()
        signals = session.query(SignalLog).order_by(SignalLog.timestamp.desc()).limit(50).all()
        
        html = "<h1>交易時段訊號記錄（最近 50 筆）</h1>"
        html += "<table border='1' style='border-collapse: collapse; width: 100%;'>"
        html += "<tr><th>時間</th><th>訊號類型</th><th>進場價</th><th>結果</th><th>損益</th></tr>"
        
        for s in signals:
            result_color = {
                'success': 'green',
                'fail': 'red',
                'neutral': 'orange',
                None: 'gray'
            }.get(s.result, 'gray')
            
            html += f"<tr>"
            html += f"<td>{s.timestamp.strftime('%Y-%m-%d %H:%M')}</td>"
            html += f"<td>{s.signal_type}</td>"
            html += f"<td>{s.entry_price:,.0f}</td>"
            html += f"<td style='color: {result_color}'>{s.result or '進行中'}</td>"
            html += f"<td>{s.profit_loss:+.1f if s.profit_loss else '-'}</td>"
            html += f"</tr>"
        
        html += "</table>"
        session.close()
        return html
    except Exception as e:
        return f"Error: {e}", 500

@app.route("/stats")
def view_stats():
    """查看統計資料"""
    try:
        stats = analyze_signals()
        if not stats:
            return "<h1>尚無統計資料</h1>", 200
        
        html = "<h1>📊 訊號統計報告</h1>"
        html += f"<p>總訊號數: {stats['total_signals']}</p>"
        html += f"<p>成功: {stats['success_count']} | 失敗: {stats['fail_count']} | 中性: {stats['neutral_count']}</p>"
        html += f"<p>整體勝率: {stats['success_rate']:.1f}%</p>"
        html += f"<p>平均損益: {stats['avg_profit']:+.1f} 點</p>"
        
        html += "<h2>各類訊號表現:</h2><ul>"
        for signal_type, data in stats['by_signal_type'].items():
            html += f"<li><b>{signal_type}</b>: "
            html += f"數量 {data['total']} | 勝率 {data['success_rate']:.1f}% | "
            html += f"平均損益 {data['avg_profit']:+.1f} 點</li>"
        html += "</ul>"
        
        return html
    except Exception as e:
        return f"Error: {e}", 500

def run_bot():
    main()

if __name__ == "__main__":
    import sys
    current_time = datetime.now()
    print("\n" + "=" * 70, flush=True)
    print("🚀 台指期開盤價基準監控系統啟動中...", flush=True)
    print("=" * 70, flush=True)
    print(f"⏰ 啟動時間: {current_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📅 星期: {['一', '二', '三', '四', '五', '六', '日'][current_time.weekday()]}", flush=True)
    
    # 判斷交易時段
    current_hour = current_time.hour
    if 8 <= current_hour < 14:
        print("🕐 當前時段: 日盤交易時間 (08:45-13:45)", flush=True)
    elif 15 <= current_hour or current_hour < 5:
        print("🌙 當前時段: 夜盤交易時間 (15:00-05:00)", flush=True)
    else:
        print("😴 當前時段: 休市時間", flush=True)
    
    print("🌐 Flask 服務準備中...", flush=True)
    print("=" * 70 + "\n", flush=True)
    sys.stdout.flush()
    
    # 延遲啟動監控執行緒，避免啟動超時
    def delayed_start():
        import time
        import sys
        time.sleep(5)  # 等待 Flask 完全啟動
        print("\n" + "=" * 70, flush=True)
        print("🤖 監控執行緒啟動中...", flush=True)
        print("=" * 70 + "\n", flush=True)
        sys.stdout.flush()  # 強制輸出
        try:
            main()
        except Exception as e:
            print(f"❌ 監控執行緒錯誤: {e}", flush=True)
            import traceback
            traceback.print_exc()
    
    t = threading.Thread(target=delayed_start, name="MonitorThread")
    t.daemon = True
    t.start()
    print(f"✅ 監控執行緒已建立 (Thread ID: {t.ident})", flush=True)
    
    # Keep-alive 也延遲啟動
    def delayed_keepalive():
        import time
        import sys
        time.sleep(10)
        
        # 自動偵測 Render URL（從環境變數）
        render_url = os.getenv('RENDER_EXTERNAL_URL')
        if not render_url:
            # 如果沒有環境變數，使用預設 URL
            render_url = "https://danny-macd.onrender.com"
        
        print(f"🔄 Keep-alive 功能啟動（每 5 分鐘自動喚醒）", flush=True)
        print(f"🌐 目標 URL: {render_url}", flush=True)
        sys.stdout.flush()
        try:
            keep_alive(render_url)
        except Exception as e:
            print(f"❌ Keep-alive 錯誤: {e}", flush=True)
    
    t2 = threading.Thread(target=delayed_keepalive, name="KeepAliveThread")
    t2.daemon = True
    t2.start()
    print(f"✅ Keep-alive 執行緒已建立 (Thread ID: {t2.ident})", flush=True)

    print("✅ Flask 服務準備就緒，開始監聽 port 10000...")
    print("=" * 70 + "\n")
    app.run(host="0.0.0.0", port=10000)