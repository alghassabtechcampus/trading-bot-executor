FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# trade_logger.py rides in the SAME image as the executor on purpose: it needs
# BYBIT_API_KEY, and this is the service that already has it. The indicators
# image deliberately carries no trading credential (see indicators/Dockerfile)
# and must stay that way, so the logger cannot live there.
#
# It is not started by the CMD below -- docker-compose.yml runs a second
# container from this image with `command: python trade_logger.py`, so the web
# service and the logger restart independently.
COPY trade_logger.py .

# The append-only fill archive must outlive the container: deploying replaces
# the image wholesale, so /data has to be a mounted volume or every release
# silently truncates the trade history. Only the directory is created here --
# the volume itself is declared in docker-compose.yml rather than as a VOLUME
# instruction, because the web service shares this image and does not need
# /data; a VOLUME here would hand every executor container a stray anonymous
# volume it never writes to.
ENV TRADE_LOGGER_DATA_DIR=/data
RUN mkdir -p /data

# Without this, Python block-buffers stdout when it is a pipe rather than a
# tty, so trade_logger's print() output sits in a 4-8KB buffer and
# `docker compose logs -f trade-logger` shows nothing for hours -- making a
# healthy logger indistinguishable from a hung one. indicators/Dockerfile sets
# this for the same reason; the root image never needed it while gunicorn
# (which writes its own unbuffered access log) was the only process here.
ENV PYTHONUNBUFFERED=1

# استراتيجية liq_flush. liq_signal.py يحمل الرياضيات (تطابق الباك-تيست حرفياً)
# وliq_flush.py يحمل التنفيذ. الاثنان يُنادَيان من مسار /liq_tick في نفس خدمة
# الويب، فلا حاوية جديدة ولا بيانات دخول جديدة.
#
# هما سبب إضافة pandas/numpy إلى requirements.txt (+~90MB على الصورة): الحساب
# في liq_signal.py متحقَّق منه مقابل الباك-تيست، وإعادة كتابته بدونهما تعني
# أرقاماً حية لا تطابق ما اختُبر.
COPY liq_signal.py liq_flush.py .

# استراتيجية الاختراق + الحَكَم: bo_signal.py الرياضيات (بلا pandas)، وbreakout.py
# التنفيذ. تُنادى من /bo_tick في نفس خدمة الويب.
COPY bo_signal.py breakout.py .

EXPOSE 5000

# استخدم gunicorn في الـ production
# --timeout 180 (كان 30): نبضة /liq_tick تفحص 17 رمزاً × 5 طلبات Bybit = ~60
# ثانية مقاسة. بمهلة 30 ثانية كان gunicorn يقتل العامل في منتصف الفحص، فتُنفَّذ
# عمليات الخروج (تجري أولاً) ولا يُنفَّذ أي دخول — بلا أي خطأ ظاهر. مهلة عقدة
# HTTP Request في n8n لازم تكون أطول من هذه أيضاً.
#
# --workers 3 (كان 2): /monitor_positions ينادى كل دقيقة، وبعاملين فقط كان عامل
# واحد مشغولاً دقيقة كاملة كل ساعة يخلّي نصف السعة مشغولة. الثالث يضمن وجود
# عامل حر دائماً — نفس هدف "Stop a slow alert check from taking the whole
# service down" (38fb75a).
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "3", "--timeout", "180", "app:app"]
