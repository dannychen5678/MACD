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
        
        if r.status_code != 200:
            return None, None, None
        
        data = r.json()
        quotes = data.get("RtData", {}).get("QuoteList", [])
        
        if not quotes:
            return None, None, None

        txf_list = [q for q in quotes if q["SymbolID"].startswith("TXF") and q["CLastPrice"]]
        
        if not txf_list:
            return None, None, None

        q = txf_list[0]
        price = float(q["CLastPrice"])
        ref_price = float(q["CRefPrice"]) if q["CRefPrice"] else price
        timestamp = datetime.now()
        
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

# === 價格下跌監控器 ===
class PriceDropMonitor:
    def __init__(self):
        self.recent_high = None
        self.recent_high_time = None
        self.notified_levels = set()
        self.alert_interval = 100  # 每跌 100 點通知
        self.min_alert_drop = 500  # 最少跌 500 點才開始通知
        self.lookback_minutes = 60  # 追蹤最近 60 分鐘的高點
    
    def update(self, df_5min):
        """
        更新價格並檢查是否需要通知
        df_5min: 5分鐘K棒資料
        """
        if len(df_5min) < 2:
            return None, None
        
        current_price = float(df_5min['close'].iloc[-1])
        current_time = df_5min.index[-1]
        
        # 計算lookback範圍內的最高點
        lookback_bars = self.lookback_minutes // 5  # 轉換為K棒數量
        recent_data = df_5min.tail(lookback_bars)
        period_high = float(recent_data['high'].max())
        
        # 初始化或更新高點
        if self.recent_high is None:
            self.recent_high = period_high
            self.recent_high_time = current_time
            return None, None
        
        # 如果創新高，重置通知記錄
        if period_high > self.recent_high:
            print(f"✅ 創新高: {self.recent_high:,.0f} → {period_high:,.0f}")
            self.recent_high = period_high
            self.recent_high_time = current_time
            self.notified_levels.clear()
            return None, None
        
        # 計算跌幅
        drop = self.recent_high - current_price
        
        # 還沒跌到最低通知門檻
        if drop < self.min_alert_drop:
            return None, None
        
        # 計算當前級距（例如：跌 650 點 → 級距 600）
        current_level = int((drop // self.alert_interval) * self.alert_interval)
        
        # 檢查是否需要通知
        if current_level not in self.notified_levels:
            self.notified_levels.add(current_level)
            
            signal_data = {
                'recent_high': self.recent_high,
                'current_price': current_price,
                'drop': drop,
                'drop_level': current_level,
                'high_time': self.recent_high_time
            }
            
            signal_type = f"價格下跌 {current_level} 點"
            
            return signal_type, signal_data
        
        return None, None

# 建立全域監控器
price_monitor = PriceDropMonitor()

def check_divergence(df):
    """
    價格下跌檢測（取代原本的背離判斷）
    跌 500 點後開始通知，之後每跌 100 點通知一次
    """
    return price_monitor.update(df)

# === 階段 1：數據收集 ===
def record_signal(signal_type, price, signal_data, df_5min):
    """記錄訊號到資料庫"""
    try:
        session = Session()
        signal = SignalLog(
            timestamp=datetime.now(),
            signal_type=signal_type,
            entry_price=price,
            slope=float(signal_data.get('drop', 0)),  # 用跌幅代替斜率
            hist_avg=float(signal_data.get('recent_high', price)),  # 用高點代替 hist_avg
            hist_now=float(signal_data.get('current_price', price)),  # 用現價代替 hist_now
            price_range=float(signal_data.get('drop', 0)),  # 用跌幅代替 price_range
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
                if '看多' in signal.signal_type or '轉多' in signal.signal_type:
                    profit_loss = current_price - signal.entry_price
                else:  # 看空
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
    print("🤖 開始監控台指期價格下跌訊號（AI 自動學習版）", flush=True)
    print("=" * 60, flush=True)
    print("📌 監控邏輯：追蹤最近 60 分鐘高點", flush=True)
    print("📌 通知規則：跌 500 點後開始通知，之後每跌 100 點通知一次", flush=True)
    print(f"📌 當前參數：lookback={price_monitor.lookback_minutes} 分鐘", flush=True)
    print("=" * 60 + "\n", flush=True)
    sys.stdout.flush()
    
    df_tick = pd.DataFrame(columns=['Close'])
    last_alert = None
    last_alert_time = datetime.min
    last_price = None
    last_record_time = None
    data_ready = False
    last_analysis_time = datetime.now()
    last_heartbeat = datetime.now()  # 心跳計時器
    loop_count = 0  # 循環計數器
    
    while True:
        loop_count += 1
        
        # 每 60 秒顯示一次心跳訊息（無論是否有價格）
        if (datetime.now() - last_heartbeat).total_seconds() >= 60:
            import sys
            print(f"💓 心跳 #{loop_count} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 監控運行中...", flush=True)
            sys.stdout.flush()
            last_heartbeat = datetime.now()
        
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
                df_tick.index = pd.to_datetime(df_tick.index, errors='coerce')
                cutoff_time = datetime.now() - timedelta(hours=48)
                df_tick = df_tick.loc[df_tick.index >= cutoff_time]
                df_tick.loc[timestamp] = price
                last_price = price
                last_record_time = timestamp
            
            df_5min = df_tick['Close'].resample('5min').ohlc()
            df_5min['volume'] = df_tick['Close'].resample('5min').count()
            df_5min.dropna(inplace=True)
            
            if len(df_5min) < 60:
                continue
            
            if not data_ready:
                data_ready = True
                print("\n" + "=" * 60)
                print("✅ 資料量已足夠，開始監控！")
                print("=" * 60)
                print(f"📊 當前有 {len(df_5min)} 根 5 分鐘 K 棒")
                print(f"📈 最新價格: {price:,.0f}")
                print(f"⚙️ 監控參數: slope={params.slope_threshold}, lookback={params.lookback}")
                print("=" * 60 + "\n")
            
            df_5min = calc_macd(df_5min)
            
            # 更新訊號結果
            update_signal_results(df_5min)
            
            # 每 30 分鐘分析一次並優化參數
            if (datetime.now() - last_analysis_time).total_seconds() >= 1800:
                stats = analyze_signals()
                if stats:
                    print_statistics(stats)
                    optimize_parameters(stats)
                last_analysis_time = datetime.now()
            
            # 檢查價格下跌訊號
            alert, signal_data = check_divergence(df_5min)
            
            # 每 3 分鐘顯示一次詳細狀態
            if data_ready and loop_count % 60 == 0:  # 每 60 個循環（約 3 分鐘）
                recent_high = price_monitor.recent_high if price_monitor.recent_high else price
                drop = recent_high - price
                print(f"📊 {datetime.now().strftime('%H:%M:%S')} | "
                      f"價格: {price:,.0f} | "
                      f"高點: {recent_high:,.0f} | "
                      f"跌幅: {drop:+.0f} | "
                      f"K棒: {len(df_5min)} | "
                      f"循環: #{loop_count}")
            
            now = datetime.now()
            cooldown = timedelta(minutes=params.cooldown_minutes)
            
            if alert and alert != last_alert and now - last_alert_time > cooldown:
                # 記錄訊號
                record_signal(alert, price, signal_data, df_5min)
                
                # 發送通知
                high_time_str = signal_data['high_time'].strftime('%H:%M:%S')
                msg = (f"⚠️ {alert}\n"
                       f"⏰ {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
                       f"📈 高點: {signal_data['recent_high']:,.0f} ({high_time_str})\n"
                       f"📉 現價: {signal_data['current_price']:,.0f}\n"
                       f"💥 跌幅: {signal_data['drop']:.0f} 點\n"
                       f"🎯 級距: {signal_data['drop_level']} 點")
                send_alert(msg)
                
                last_alert = alert
                last_alert_time = now
                print(f"\n🔔 發送警報: {alert}\n")
        
        time.sleep(3)


app = Flask(__name__)

@app.route("/")
def home():
    return "Service is running (AI Learning Version)", 200

@app.route("/health")
def health():
    """健康檢查端點 - 快速回應"""
    return {"status": "ok", "service": "macd-monitor", "timestamp": datetime.now().isoformat()}, 200

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
        
        html = "<h1>MACD 訊號記錄（最近 50 筆）</h1>"
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
    print("🚀 MACD 監控系統啟動中...", flush=True)
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
        print("🔄 Keep-alive 功能啟動（每 10 分鐘自動喚醒）", flush=True)
        sys.stdout.flush()
        try:
            keep_alive("https://danny-macd.onrender.com")
        except Exception as e:
            print(f"❌ Keep-alive 錯誤: {e}", flush=True)
    
    t2 = threading.Thread(target=delayed_keepalive, name="KeepAliveThread")
    t2.daemon = True
    t2.start()
    print(f"✅ Keep-alive 執行緒已建立 (Thread ID: {t2.ident})", flush=True)

    print("✅ Flask 服務準備就緒，開始監聽 port 10000...")
    print("=" * 70 + "\n")
    app.run(host="0.0.0.0", port=10000)
