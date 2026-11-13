FROM python:3.12-slim
WORKDIR /srv
# 見た目プレビュー用: LibreOffice(docx/pptx/xlsx→PDF) + poppler(PDF→画像) + CJKフォント
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-impress libreoffice-calc \
      poppler-utils poppler-data fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi "uvicorn[httptools]" httpx python-multipart python-docx python-pptx openpyxl babeldoc markdown
COPY app/ ./app/
ENV PYTHONUNBUFFERED=1 PORT=9130
EXPOSE 9130
CMD ["python", "app/main.py"]
