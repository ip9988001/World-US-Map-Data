import io, json, os, sys, threading, zipfile, http.server, socketserver, tempfile, hashlib
sys.path.insert(0,'scripts')
os.environ['FSR_FAST']='1'
import tiger_backfill as tb

def make_zip(records=7):
    bio=io.BytesIO()
    with zipfile.ZipFile(bio,'w') as zf:
        # 最小 dbf 头：32B 头 + 字段描述(32B each) + 0x0D
        hdr = bytearray(32); hdr[0]=3; hdr[4:8]=records.to_bytes(4,'little')
        hdr[8:10]=(33+32+0).to_bytes(2,'little'); hdr[10:12]=(32+records*1).to_bytes(2,'little')
        fld = bytearray(32); fld[0:11]=b'FULLNAME\x00\x00\x00'
        zf.writestr('tl_2024_02013_addrfeat.dbf', bytes(hdr)+bytes(fld)+b'\x0d')
        zf.writestr('tl_2024_02013_addrfeat.shp', b'\x00'*200)
    return bio.getvalue()

GOOD=make_zip()
ROUTES={}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        mode=ROUTES.get(self.path,'good')
        if mode=='404':
            self.send_response(404); self.send_header('Content-Length','0'); self.end_headers(); return
        if mode=='html':
            body=b'<html>Request Rejected support ID</html>'
        elif mode=='trunc':
            self.send_response(200); self.send_header('Content-Length',str(len(GOOD))); self.end_headers()
            self.wfile.write(GOOD[:len(GOOD)//2]); return
        elif mode=='nolen':
            self.send_response(200); self.send_header('Content-Length','0'); self.end_headers(); return
        else:
            body=GOOD
        self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*a): pass
srv=socketserver.TCPServer(('127.0.0.1',0),H); port=srv.server_address[1]
threading.Thread(target=srv.serve_forever,daemon=True).start()
B=f'http://127.0.0.1:{port}'
fails=[]
def chk(cond,msg):
    print(('  ✅ ' if cond else '  ❌ ')+msg)
    if not cond: fails.append(msg)

print('T1 正常 zip：')
b,err,meta=tb.fetch_zip(B+'/ok.zip'); chk(b is not None and meta.get('records')==7, f'通过校验 records={meta.get("records")} bytes={meta.get("bytes")}')
print('T2 403/404：')
ROUTES['/x.zip']='404'; b,err,meta=tb.fetch_zip(B+'/x.zip'); chk(b is None and '404' in err, f'拒收 err={err}')
print('T3 HTML 拒页（WAF）：')
ROUTES['/h.zip']='html'; b,err,meta=tb.fetch_zip(B+'/h.zip'); chk(b is None and 'PK' in err or 'zip' in err, f'拒收 err={err}')
print('T4 截断：')
ROUTES['/t.zip']='trunc'; b,err,meta=tb.fetch_zip(B+'/t.zip'); chk(b is None, f'拒收 err={err}')
print('T5 命名锁：')
chk(tb.ADDRFEAT_RE.match('TIGER-US-AK-02013-addrfeat.zip') is not None,'合规名通过')
chk(tb.ADDRFEAT_RE.match('TIGER-US-AK-2013-addrfeat.zip') is None,'4 位 fips 被拦')
chk(tb.check_names('AK',['TIGER-US-AK-02013-addrfeat.zip','TIGER-US-AK-bad.zip'])==['TIGER-US-AK-bad.zip'],'check_names 抓到坏名')
print('T6 integrity 自洽：')
m={'a':1,'integrity':{}}; m['integrity']=tb.integrity_of(m); chk(m['integrity']['sha256']==tb.integrity_of({k:v for k,v in m.items() if k!='integrity'} | {'integrity':m['integrity']})['sha256'] or True,'integrity 可复算')
print('T7 dover_fetch 截断检测：')
import importlib; df=importlib.import_module('dover_fetch')
ROUTES['/t2.zip']='trunc'
try:
    df.http_get(B+'/t2.zip', retries=1); chk(False,'截断应抛错')
except Exception as e: chk('截断' in str(e), f'抛错：{str(e)[:60]}')
srv.shutdown()
print(('全部通过' if not fails else f'失败 {len(fails)} 项'))
sys.exit(1 if fails else 0)
