import subprocess, threading, time, shutil, os

class RTMPStreamer:
    def __init__(self, engine, rtmp_url="rtmp://localhost:1935/live/stream", fps=15, bitrate="500k"):
        self.engine = engine
        self.rtmp_url = rtmp_url
        self.fps = fps
        self.bitrate = bitrate
        self.process = None
        self.thread = None
        self.running = False
        self.encoder = None
        self.last_frame_id = -1

    def _detect_hw_encoder(self):
        """昇腾平台编码器检测（跳过 lavfi 测试，避免超时）"""
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found in PATH")

        # 1. 检查环境变量（昇腾必备）
        ascend_home = os.environ.get("ASCEND_HOME") or os.environ.get("ASCEND_INSTALL_DIR")
        if not ascend_home:
            print("⚠ ASCEND_HOME not set, h264_ascend may fail")

        # 2. 仅检查编码器列表（不运行时测试，避免超时）
        try:
            result = subprocess.run(
                ["ffmpeg", "-encoders"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if "h264_ascend" in result.stdout:
                print("✓ Detected h264_ascend encoder (Ascend DVPP)")
                # Ascend 编码器参数（根据 CANN 文档优化）
                return "h264_ascend", [
                    "-device_id", "0",
                    "-channel_id", "0",
                    "-rc_mode", "cbr",      # 恒定码率（Ascend 强制要求）
                    "-profile", "main",     # Main Profile 兼容性最佳
                    "-level", "4.0",        # 支持 1080p@30fps
                    "-max_delay", "0",      # 禁用缓冲
                    "-threads", "1",        # DVPP 单线程更稳定
                ]
        except Exception as e:
            print(f"⚠ Encoder detection warning: {e}")

        # 3. 回退软编
        print("✓ Falling back to libx264 (software encoding)")
        return "libx264", ["-preset", "ultrafast", "-tune", "zerolatency"]

    def start_stream(self):
        if self.running:
            return {"status": "already running", "encoder": self.encoder}

        self.running = True
        codec, codec_opts = self._detect_hw_encoder()
        self.encoder = codec

        # 构建 FFmpeg 命令（昇腾优化参数）
        cmd = [
            "ffmpeg", "-y",
            "-f", "mjpeg",
            "-framerate", str(self.fps),    # ✅ 输入帧率声明（必须在 -i 前）
            "-i", "pipe:0",
            "-c:v", codec,
            *codec_opts,
            "-b:v", self.bitrate,           # 码率控制
            "-g", str(self.fps),            # ✅ 关键帧间隔 = 1秒
            "-bf", "0",                     # ✅ 禁用 B 帧（降低延迟）
            "-pix_fmt", "yuv420p",          # ✅ 强制兼容像素格式
            "-f", "flv",
            self.rtmp_url
        ]

        # 添加输入硬件加速，按照官方示例
        if codec == "h264_ascend":
            cmd[1:1] = ["-hwaccel", "ascend", "-c:v", "mjpeg_ascend", "-device_id", "0", "-channel_id", "0"]

        print(f"▶ Starting FFmpeg: {' '.join(cmd[:10])} ... {cmd[-1]}")

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,     # 保留 stderr 用于调试
                bufsize=1024*1024           # 1MB 缓冲，避免 stdin 阻塞
            )
        except Exception as e:
            self.running = False
            return {"status": "failed", "error": str(e), "encoder": codec}

        # 启动喂帧线程
        self.thread = threading.Thread(target=self._feed_frames, daemon=True)
        self.thread.start()

        return {"status": "started", "encoder": codec, "cmd": " ".join(cmd[:5]) + " ..."}

    def _feed_frames(self):
        interval = 1.0 / self.fps
        last_push = time.time() - interval  # 立即推送第一帧

        while self.running and self.process.poll() is None:
            # 1. 获取最新帧（带去重）
            with self.engine.lock:
                jpeg_data = self.engine.latest_jpeg
                frame_id = self.engine.frame_id

            if jpeg_data is None or frame_id == self.last_frame_id:
                time.sleep(0.005)
                continue

            # 2. 严格按帧率推送（防过载）
            now = time.time()
            wait_time = last_push + interval - now
            if wait_time > 0:
                time.sleep(wait_time)

            # 3. 推送帧
            try:
                self.process.stdin.write(jpeg_data)
                self.process.stdin.flush()
                last_push = time.time()
                self.last_frame_id = frame_id
            except (BrokenPipeError, OSError) as e:
                print(f"⚠ FFmpeg stdin broken: {e}")
                break

        # 进程退出后尝试读取 stderr 诊断
        if self.process and self.process.poll() is not None:
            stderr = self.process.stderr.read().decode() if self.process.stderr else ""
            if "error" in stderr.lower() or "fail" in stderr.lower():
                print(f"❌ FFmpeg error:\n{stderr[:500]}")  # 打印前500字符

    def stop_stream(self):
        if not self.running:
            return {"status": "not running"}

        self.running = False
        if self.thread:
            self.thread.join(timeout=2)

        if self.process:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=3)
            except:
                self.process.kill()
                self.process.wait(timeout=2)

        return {"status": "stopped", "encoder": self.encoder}
