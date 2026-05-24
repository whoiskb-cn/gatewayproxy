import time
import httpx
from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel

def verify_token(authorization: str = Header(None)):
    if not authorization or authorization != "Bearer nex_gateway_secure_2026":
        raise HTTPException(status_code=401, detail="Unauthorized")

router = APIRouter(prefix="/api/cookie_115", tags=["cookie_115"], dependencies=[Depends(verify_token)])

# --- 全局常量 ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Referer": "https://115.com/"
}

class QrappTypePayload(BaseModel):
    app_type: str

class CheckStatusPayload(BaseModel):
    uid: str
    time: int
    sign: str
    api_app_type: str

class GetCookiePayload(BaseModel):
    uid: str
    api_app_type: str

@router.post("/get_qrcode")
async def get_qrcode(data: QrappTypePayload):
    try:
        app_type = data.app_type
        
        api_app_type = app_type
        if app_type == "android":
            api_app_type = "115android"
        elif app_type == "ios":
            api_app_type = "115ios"
            
        current_headers = HEADERS.copy()
        if "android" in api_app_type or "ios" in api_app_type:
            current_headers["User-Agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 115Browser/30.0.0"
            
        token_url = f"https://qrcodeapi.115.com/api/1.0/{api_app_type}/1.0/token/"
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = (await client.get(token_url, headers=current_headers)).json()
        
        if not token_resp.get("state"):
            return {'success': False, 'message': token_resp.get('message', '获取二维码失败')}
            
        qr_data = token_resp["data"]
        uid = qr_data["uid"]
        qr_link = f"https://qrcodeapi.115.com/api/1.0/{api_app_type}/1.0/qrcode?uid={uid}"
        
        return {
            'success': True,
            'qr_url': qr_link,
            'uid': uid,
            'time': qr_data['time'],
            'sign': qr_data['sign'],
            'api_app_type': api_app_type
        }
    except Exception as e:
        return {'success': False, 'message': f'服务器错误: {str(e)}'}

@router.post("/check_status")
async def check_status(data: CheckStatusPayload):
    try:
        current_headers = HEADERS.copy()
        if "android" in data.api_app_type or "ios" in data.api_app_type:
            current_headers["User-Agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 115Browser/30.0.0"
            
        params = {
            "uid": data.uid,
            "time": data.time,
            "sign": data.sign,
            "_": int(time.time() * 1000)
        }
        async with httpx.AsyncClient(timeout=10) as client:
            res_obj = await client.get("https://qrcodeapi.115.com/get/status/", params=params, headers=current_headers)
            res = res_obj.json()
        status = res.get("data", {}).get("status")
        
        return {
            'success': True,
            'status': status
        }
    except Exception as e:
        return {'success': False, 'message': f'服务器错误: {str(e)}'}

@router.post("/get_cookie")
async def get_cookie(data: GetCookiePayload):
    try:
        current_headers = HEADERS.copy()
        if "android" in data.api_app_type or "ios" in data.api_app_type:
            current_headers["User-Agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 115Browser/30.0.0"
            
        result_url = f"https://passportapi.115.com/app/1.0/{data.api_app_type}/1.0/login/qrcode/"
        payload = {"app": data.api_app_type, "account": data.uid}
        async with httpx.AsyncClient(timeout=10) as client:
            result_resp_obj = await client.post(result_url, data=payload, headers=current_headers)
            result_resp = result_resp_obj.json()
        
        if result_resp.get("state"):
            cookies = result_resp['data']['cookie']
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            return {
                'success': True,
                'cookie': cookie_str
            }
        else:
            return {'success': False, 'message': result_resp.get('message', '获取Cookie失败')}
    except Exception as e:
        return {'success': False, 'message': f'服务器错误: {str(e)}'}
