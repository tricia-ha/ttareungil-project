"""현재 재고 + 학습한 시간별 대여/반납 강도 -> 도착시 재고 0 확률.

0에서는 대여가 막히고 반납은 허용되는 birth-death CTMC. 반납 상한 없음.
이 확률은 모형 가정하 추정치이며 실제 재고 이력으로 보정되지 않았다.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import csv, json, math, argparse
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
KST=timezone(timedelta(hours=9))

def parse_time(value):
    t=datetime.fromisoformat(value) if isinstance(value,str) else value
    return t.replace(tzinfo=KST) if t.tzinfo is None else t.astimezone(KST)

def advance(p,rent,ret,hours):
    q=rent+ret
    if q==0 or hours==0:return p.copy()
    # uniformization: exp(Qt)p. 짧은 구간으로 나누어 수치 오차를 줄인다.
    pieces=max(1,math.ceil(q*hours/20));x=q*hours/pieces
    for _ in range(pieces):
        v=p.copy();w=math.exp(-x);out=w*v
        for k in range(1,math.ceil(x+12*math.sqrt(x)+40)):
            nxt=np.zeros_like(v)
            nxt[:-1]+=v[1:]*rent/q;nxt[0]+=v[0]*rent/q
            nxt[1:]+=v[:-1]*ret/q;nxt[-1]+=v[-1]*ret/q
            v=nxt;w*=x/k;out+=w*v
        p=out
    return p/np.sum(p)

class InventoryPredictor:
    def __init__(self,rates_path=None):
        rates_path=rates_path or ROOT/'03_모델/weekly_rates.csv'
        with open(rates_path,encoding='utf-8-sig',newline='') as f:
            self.rates={(r['station_id'],int(r['dow']),int(r['hour'])):(float(r['rentals_per_hour']),float(r['returns_per_hour'])) for r in csv.DictReader(f)}
        activity_path=Path(rates_path).parent/'station_activity.csv'
        if not activity_path.exists():raise ValueError('station_activity.csv가 필요합니다. 모델 패키지를 함께 복사하세요.')
        with activity_path.open(encoding='utf-8-sig',newline='') as f:
            active={row['station_id'] for row in csv.DictReader(f) if float(row['train_flow_total'])>0}
        self.station_ids={k[0] for k in self.rates}&active
    def predict(self,station_id,current_bikes,eta_minutes,as_of,snapshot_at=None,epsilon=.10,rental_multiplier=1.,return_multiplier=1.):
        if station_id not in self.station_ids:raise ValueError('모델 범위 밖 대여소입니다.')
        if isinstance(current_bikes,bool) or not isinstance(current_bikes,(int,np.integer)) or not 0<=current_bikes<=500:raise ValueError('current_bikes는 0~500의 정수여야 합니다.')
        if not math.isfinite(float(eta_minutes)) or not 0<=eta_minutes<=60:raise ValueError('eta_minutes는 0~60분이어야 합니다.')
        if not 0<epsilon<1:raise ValueError('epsilon은 0과 1 사이여야 합니다.')
        if not all(math.isfinite(float(x)) and 0<=x<=5 for x in [rental_multiplier,return_multiplier]):raise ValueError('수요 배수는 0~5여야 합니다.')
        start=parse_time(as_of)
        if snapshot_at:
            age=(start-parse_time(snapshot_at)).total_seconds()
            if not 0<=age<=600:raise ValueError('재고 관측은 예측 기준시각보다 과거 10분 이내여야 합니다.')
        end=start+timedelta(minutes=float(eta_minutes));t=start;segments=[];mr=mt=0.
        while t<end:
            boundary=t.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1)
            finish=min(boundary,end);hours=(finish-t).total_seconds()/3600
            r,b=self.rates[station_id,t.weekday(),t.hour];r*=rental_multiplier;b*=return_multiplier
            segments.append((r,b,hours));mr+=r*hours;mt+=b*hours;t=finish
        n=current_bikes+max(80,math.ceil(mt+12*math.sqrt(mt+1)+30));p=np.zeros(n+1);p[current_bikes]=1
        for r,b,h in segments:p=advance(p,r,b,h)
        cdf=np.cumsum(p)
        quant=lambda v:int(np.searchsorted(cdf,v))
        # 반납이 전혀 없을 때의 위험. 동일 대여 강도하 보수적 비교값.
        if current_bikes==0:upper=1.
        else:
            term=math.exp(-mr);cdf_r=term
            for k in range(1,current_bikes):term*=mr/k;cdf_r+=term
            upper=max(0.,min(1.,1-cdf_r))
        risk=float(p[0])
        return {'station_id':station_id,'as_of':start.isoformat(),'arrival_at':end.isoformat(),'current_bikes':int(current_bikes),'eta_minutes':float(eta_minutes),'expected_rentals':mr,'expected_returns':mt,'p_empty_at_arrival':risk,'p_available_at_arrival':1-risk,'expected_inventory_at_arrival':float(np.dot(np.arange(len(p)),p)),'inventory_interval_90':[quant(.05),quant(.95)],'no_return_risk':upper,'epsilon':epsilon,'passes_risk_constraint':risk<=epsilon,'probability_status':'assumption_based_uncalibrated','inventory_source':'api_snapshot' if snapshot_at else 'manual_scenario','scope':'광진구 중구 마포구','model_assumptions':['independent Poisson flows','hourly constant intensity','no redistribution','no return capacity ceiling','observed completed rentals proxy demand']}

def main():
    p=argparse.ArgumentParser();p.add_argument('--station',required=True);p.add_argument('--bikes',type=int,required=True);p.add_argument('--eta',type=float,default=15);p.add_argument('--time',default=datetime.now(KST).isoformat());a=p.parse_args()
    print(json.dumps(InventoryPredictor().predict(a.station,a.bikes,a.eta,a.time),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
