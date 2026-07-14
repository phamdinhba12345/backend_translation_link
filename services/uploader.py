def upload_to_youtube(video_path: str):
    """
    Tích hợp YouTube Data API v3 để upload video.
    """
    print(f"Uploading {video_path} to YouTube...")
    # Cần cấu hình google-auth, google-api-python-client
    return {"platform": "youtube", "status": "success", "url": "https://youtube.com/watch?v=demo"}

def upload_to_facebook(video_path: str):
    """
    Tích hợp Facebook Graph API để upload video lên Page.
    """
    print(f"Uploading {video_path} to Facebook...")
    # Cần Access Token của Page với quyền publish_video
    return {"platform": "facebook", "status": "success", "url": "https://facebook.com/demo"}

def upload_to_tiktok(video_path: str):
    """
    Tích hợp TikTok Content Posting API hoặc tự động hóa (Selenium/Playwright).
    """
    print(f"Uploading {video_path} to TikTok...")
    return {"platform": "tiktok", "status": "success", "url": "https://tiktok.com/@demo/video/123"}

def upload_to_socials(video_path: str, platforms: list[str]) -> list[dict]:
    """
    Upload video lên các nền tảng mạng xã hội được yêu cầu.
    """
    results = []
    if "youtube" in platforms:
        results.append(upload_to_youtube(video_path))
    if "facebook" in platforms:
        results.append(upload_to_facebook(video_path))
    if "tiktok" in platforms:
        results.append(upload_to_tiktok(video_path))
    
    return results
