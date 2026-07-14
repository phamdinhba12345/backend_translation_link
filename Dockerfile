FROM python:3.10-slim

# Cài đặt các thư viện hệ thống cần thiết, đặc biệt là ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Thiết lập thư mục làm việc
WORKDIR /app

# Copy file requirements.txt và cài đặt thư viện Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cài đặt các trình duyệt cho Playwright (nếu thư viện của bạn sử dụng Playwright để cào dữ liệu)
RUN playwright install --with-deps chromium

# Copy toàn bộ mã nguồn vào container
COPY . .

# Render sẽ tự động cấu hình biến môi trường PORT (thường là 10000)
ENV PORT=10000
EXPOSE $PORT

# Khởi chạy server Flask bằng Gunicorn. 
# Cấu hình --timeout 0 giúp Gunicorn không kill worker khi xử lý video dịch lâu.
CMD gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 0
