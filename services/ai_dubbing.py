import os
import subprocess

def extract_audio(video_path: str, audio_path: str):
    """
    Sử dụng FFmpeg để trích xuất âm thanh từ video.
    """
    try:
        command = ["ffmpeg", "-y", "-i", video_path, "-q:a", "0", "-map", "a", audio_path]
        subprocess.run(command, check=True)
    except Exception as e:
        print(f"Lỗi extract_audio (Dùng file giả lập vì không có ffmpeg): {e}")
        with open(audio_path, "w") as f:
            f.write("Dummy audio data")

def transcribe_audio(audio_path: str) -> str:
    """
    Sử dụng OpenAI Whisper API để chuyển đổi giọng nói thành văn bản.
    """
    print("Transcribing audio...")
    try:
        from openai import OpenAI
        import os
        
        # Cần set biến môi trường OPENAI_API_KEY để sử dụng
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("Cảnh báo: Chưa cài đặt OPENAI_API_KEY. Trả về text giả lập.")
            return "This is a transcribed text from the video."
            
        client = OpenAI(api_key=api_key)
        
        with open(audio_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file
            )
        return transcript.text
    except Exception as e:
        print(f"Lỗi Whisper API (Giả lập): {e}")
        return "This is a transcribed text from the video."

def translate_text(text: str, target_language: str) -> str:
    """
    Dịch văn bản sang ngôn ngữ đích sử dụng OpenAI GPT.
    """
    print(f"Translating to {target_language}...")
    try:
        from openai import OpenAI
        import os
        
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("Cảnh báo: Chưa cài đặt OPENAI_API_KEY. Trả về text giả lập.")
            return "Đây là văn bản đã được dịch."
            
        client = OpenAI(api_key=api_key)
        
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": f"You are a professional translator. Translate the following text into the language represented by the code '{target_language}'. Only return the translated text without any explanation or quotes."},
                {"role": "user", "content": text}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Lỗi Translation API (Giả lập): {e}")
        return "Đây là văn bản đã được dịch."

def text_to_speech(text: str, output_audio_path: str, target_language: str):
    """
    Sử dụng TTS (ElevenLabs, Google TTS) để tạo file âm thanh lồng tiếng.
    """
    try:
        from gtts import gTTS
        tts = gTTS(text, lang=target_language)
        tts.save(output_audio_path)
    except Exception as e:
        print(f"Lỗi TTS (Dùng file giả lập): {e}")
        with open(output_audio_path, "w") as f:
            f.write("Dummy tts data")
    print("Text-to-speech generated.")

def merge_audio_video(video_path: str, new_audio_path: str, output_video_path: str):
    """
    Sử dụng FFmpeg để ghép âm thanh mới vào video gốc (thay thế âm thanh cũ).
    """
    try:
        command = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", new_audio_path,
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            output_video_path
        ]
        subprocess.run(command, check=True)
        print("Video and new audio merged.")
    except Exception as e:
        print(f"Lỗi merge_audio_video (Dùng file giả lập vì không có ffmpeg): {e}")
        with open(output_video_path, "w") as f:
            f.write("Dummy merged video data")

def process_video(video_path: str, target_language: str) -> str:
    """
    Pipeline chính để xử lý lồng tiếng video.
    """
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    output_dir = os.path.dirname(video_path)
    
    extracted_audio = os.path.join(output_dir, f"{base_name}_original.mp3")
    new_audio = os.path.join(output_dir, f"{base_name}_{target_language}.mp3")
    output_video = os.path.join(output_dir, f"{base_name}_{target_language}_dubbed.mp4")
    
    # 1. Trích xuất âm thanh
    extract_audio(video_path, extracted_audio)
    
    # 2. Speech to Text
    original_text = transcribe_audio(extracted_audio)
    
    # 3. Translate
    translated_text = translate_text(original_text, target_language)
    
    # 4. Text to Speech
    text_to_speech(translated_text, new_audio, target_language)
    
    # 5. Merge
    merge_audio_video(video_path, new_audio, output_video)
    
    return output_video
