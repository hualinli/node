// ================= 全局变量与状态 =================
const urlParams = new URLSearchParams(window.location.search);
const token = urlParams.get("token") || "";

let streamVersion = 0;
let heatmapData = [];
let heatmapInstance = null;
let isRunning = false;
let isConnected = false;
let localRemainingSeconds = 0;
let classroomList = [];
let wasRunning = false;
let anomalyPoints = [];

// ================= 初始化 =================
document.addEventListener("DOMContentLoaded", () => {
    initClock();
    initTimer();
    initUIListeners();
    bootstrap();
});

function bootstrap() {
    console.log("前端应用初始化...");
    loadClassrooms();
    loadAnomalyImages(50); // 默认50条
    startStatusPolling();
    updateStreamStatus();
}

// ================= UI 基础功能 (时钟/计时器) =================
function initClock() {
    const updateTime = () => {
        const now = new Date();
        const clockEl = document.getElementById("clock");
        if (clockEl) {
            clockEl.innerHTML = now.toLocaleTimeString("zh-CN", {
                hour12: false,
            });
        }
    };
    setInterval(updateTime, 1000);
    updateTime();
}

function initTimer() {
    setInterval(() => {
        if (isRunning && localRemainingSeconds > 0) {
            localRemainingSeconds--;
            updateTimerDisplay();
            if (localRemainingSeconds <= 0) {
                console.log("考试时长已到，自动结束...");
                confirmDisconnect();
            }
        }
    }, 1000);
}

function updateTimerDisplay() {
    const timerDisplay = document.getElementById("timer-display");
    if (!timerDisplay) return;

    const h = Math.floor(localRemainingSeconds / 3600);
    const m = Math.floor((localRemainingSeconds % 3600) / 60);
    const s = Math.floor(localRemainingSeconds % 60);
    const timeStr = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;

    timerDisplay.innerText = timeStr;
}

// ================= API 接口对接 =================

/**
 * 加载教室列表并渲染下拉菜单
 */
async function loadClassrooms() {
    try {
        console.log("正在获取教室列表...");
        const resp = await fetch(`/classrooms?token=${token}`);
        if (!resp.ok) {
            console.error("加载教室列表失败:", resp.status);
            return;
        }
        const result = await resp.json();
        if (!result.success || !Array.isArray(result.classrooms)) {
            console.error("教室数据格式错误:", result);
            return;
        }

        classroomList = result.classrooms;
        const dropdown = document.getElementById("classroom-dropdown");
        if (!dropdown) return;
        dropdown.innerHTML = "";

        // 按 building 分组
        const groupedData = {};
        classroomList.forEach((item) => {
            const groupName = item.building || "其他";
            if (!groupedData[groupName]) {
                groupedData[groupName] = [];
            }
            groupedData[groupName].push(item);
        });

        // 渲染分组 HTML
        Object.keys(groupedData).forEach((groupName, index) => {
            const items = groupedData[groupName];

            const groupHeader = document.createElement("div");
            groupHeader.className = "dropdown-group-header";
            groupHeader.innerHTML = `
                <span>${groupName}</span>
                <span class="group-arrow">▶</span>
            `;
            groupHeader.onclick = (e) => {
                e.stopPropagation();
                toggleGroup(groupHeader);
            };
            dropdown.appendChild(groupHeader);

            const itemsContainer = document.createElement("div");
            itemsContainer.className = "dropdown-group-items";

            if (index === 0) {
                groupHeader.classList.add("expanded");
                itemsContainer.classList.add("expanded");
            }

            items.forEach((c) => {
                const itemDiv = document.createElement("div");
                itemDiv.className = "dropdown-item";
                itemDiv.textContent = c.name || c.id;
                itemDiv.onclick = (e) => {
                    e.stopPropagation();
                    selectClassroom(c.id, c.name || c.id);
                };
                itemsContainer.appendChild(itemDiv);
            });

            dropdown.appendChild(itemsContainer);
        });
        console.log("教室列表渲染完成");
    } catch (e) {
        console.error("加载教室请求异常:", e);
    }
}

function toggleGroup(headerElement) {
    headerElement.classList.toggle("expanded");
    const itemsContainer = headerElement.nextElementSibling;
    if (itemsContainer) {
        itemsContainer.classList.toggle("expanded");
    }
}

/**
 * 启动状态轮询
 */
function startStatusPolling() {
    setInterval(async () => {
        try {
            const resp = await fetch(`/exam/status?token=${token}`);
            if (!resp.ok) return;
            const status = await resp.json();

            isRunning = status.exam_running;

            // 更新UI字段从后端状态
            if (status.exam_running) {
                const subjectInput = document.getElementById("subject-input");
                if (subjectInput) subjectInput.value = status.subject || "";

                const classroomSelect =
                    document.getElementById("classroom-select");
                if (classroomSelect)
                    classroomSelect.value = status.classroom_id || "";

                const textSpan = document.getElementById("classroom-text");
                if (textSpan && classroomList.length > 0) {
                    const classroom = classroomList.find(
                        (c) => c.id == status.classroom_id,
                    );
                    if (classroom)
                        textSpan.textContent = classroom.name || classroom.id;
                }
            }

            // 考试刚开始时同步时间
            if (isRunning && !wasRunning) {
                localRemainingSeconds = status.remaining_seconds;
                updateTimerDisplay();
            }
            // 考试停止时清除前端展示的异常图片
            if (!isRunning && wasRunning) {
                const marqueeTrack = document.getElementById("marquee-track");
                if (marqueeTrack) marqueeTrack.innerHTML = "";
                const overlay = document.getElementById("video-overlay");
                if (overlay) {
                    overlay
                        .querySelectorAll(".anomaly-point")
                        .forEach((e) => e.remove());
                }
                // 清除热力图
                if (heatmapInstance) {
                    heatmapInstance.setData({ max: 50, data: [] });
                }
            }
            wasRunning = isRunning;

            // 同步考试倒计时：只在误差超过5秒时同步，避免频繁更新导致不流畅
            if (status.exam_running) {
                const serverRemaining = status.remaining_seconds;
                if (Math.abs(localRemainingSeconds - serverRemaining) > 5) {
                    localRemainingSeconds = serverRemaining;
                    updateTimerDisplay();
                }
            } else {
                // 考试不运行时，重置本地剩余时间
                if (localRemainingSeconds > 0) {
                    localRemainingSeconds = 0;
                    updateTimerDisplay();
                }
            }

            updateStreamStatus();
            updateButtonStates();

            // 如果考试正在运行，定期刷新异常图片
            if (isRunning) {
                const imgCountSelect =
                    document.getElementById("img-count-select");
                const count = imgCountSelect
                    ? parseInt(imgCountSelect.value)
                    : 0;
                if (count > 0) {
                    loadAnomalyImages(count);
                }
            }

            if (isRunning && !isConnected) {
                connectStream();
            } else if (!isRunning && isConnected) {
                stopStream();
            }

            // 轮询异常数据并更新热力图
            if (isRunning) {
                updateHeatmap();
            }
        } catch (e) {
            console.error("轮询状态失败:", e);
        }
    }, 2000);
}

function updateButtonStates() {
    const btnStart = document.getElementById("btn-start");
    const btnDisconnect = document.getElementById("btn-disconnect");
    const btnResetTracks = document.getElementById("btn-reset-tracks");
    const btnResetStats = document.getElementById("btn-reset-stats");
    const subjectInput = document.getElementById("subject-input");
    const durationSelect = document.getElementById("exam-duration");

    if (isRunning) {
        if (btnStart) btnStart.disabled = true;
        if (btnDisconnect) btnDisconnect.disabled = false;
        if (btnResetTracks) btnResetTracks.disabled = false;
        if (btnResetStats) btnResetStats.disabled = false;
        if (subjectInput) subjectInput.disabled = true;
        if (durationSelect) durationSelect.disabled = true;
        // 禁用教室选择
        const classroomTrigger = document.getElementById("classroom-trigger");
        if (classroomTrigger) classroomTrigger.style.pointerEvents = "none";
    } else {
        if (btnStart) btnStart.disabled = false;
        if (btnDisconnect) btnDisconnect.disabled = true;
        if (btnResetTracks) btnResetTracks.disabled = true;
        if (btnResetStats) btnResetStats.disabled = true;
        if (subjectInput) subjectInput.disabled = false;
        if (durationSelect) durationSelect.disabled = false;
        // 启用教室选择
        const classroomTrigger = document.getElementById("classroom-trigger");
        if (classroomTrigger) classroomTrigger.style.pointerEvents = "auto";
    }
}

function updateStreamStatus() {
    const statusEl = document.getElementById("stream-status");
    const overlay = document.getElementById("video-overlay");
    if (!statusEl || !overlay) return;

    if (isConnected) {
        statusEl.innerText = "已连接";
        statusEl.className = "status-connected";
        overlay.style.display = "none";
    } else {
        statusEl.innerText = "未连接";
        statusEl.className = "status-disconnected";
        overlay.style.display = "flex";
    }
}

function connectStream() {
    const videoFeed = document.getElementById("video-feed");
    if (videoFeed) {
        streamVersion++;
        videoFeed.src = `/stream?token=${token}&v=${streamVersion}`;
        isConnected = true;
        updateStreamStatus();
    }
}

function stopStream() {
    const videoFeed = document.getElementById("video-feed");
    if (videoFeed) {
        videoFeed.src = "";
        isConnected = false;
        updateStreamStatus();
    }
}

// ================= UI 交互逻辑 =================

function initUIListeners() {
    const durationSelect = document.getElementById("exam-duration");
    if (durationSelect) {
        localRemainingSeconds = parseInt(durationSelect.value) * 60;
        updateTimerDisplay();

        durationSelect.addEventListener("change", (e) => {
            if (!isRunning) {
                localRemainingSeconds = parseInt(e.target.value) * 60;
                updateTimerDisplay();
            }
        });
    }

    // 监听异常图片数量选择
    const imgCountSelect = document.getElementById("img-count-select");
    if (imgCountSelect) {
        imgCountSelect.addEventListener("change", (e) => {
            const count = parseInt(e.target.value);
            if (count > 0) {
                loadAnomalyImages(count);
            } else {
                // 关闭时清空
                updateCarousel([]);
                updatePoints([]);
            }
        });
    }

    // 点击外部区域关闭教室下拉菜单
    window.addEventListener("click", (e) => {
        const dropdown = document.getElementById("classroom-dropdown");
        const trigger = document.getElementById("classroom-trigger");
        if (
            dropdown &&
            !dropdown.contains(e.target) &&
            trigger &&
            !trigger.contains(e.target)
        ) {
            dropdown.classList.remove("show");
        }
    });
}

/**
 * 切换教室下拉菜单显示状态
 */
window.toggleDropdown = function (e) {
    if (e) e.stopPropagation();
    const dropdown = document.getElementById("classroom-dropdown");
    if (dropdown) {
        dropdown.classList.toggle("show");
    }
};

/**
 * 选择特定教室
 */
window.selectClassroom = function (id, name) {
    const input = document.getElementById("classroom-select");
    const textSpan = document.getElementById("classroom-text");
    if (input) input.value = id;
    if (textSpan) textSpan.textContent = name;

    const dropdown = document.getElementById("classroom-dropdown");
    if (dropdown) dropdown.classList.remove("show");
};

/**
 * 开始考试流程
 */
window.startExam = async function () {
    const subject = document.getElementById("subject-input").value;
    const duration = document.getElementById("exam-duration").value;
    const classroom = document.getElementById("classroom-text").textContent;

    if (!subject) {
        alert("请输入考试科目");
        return;
    }

    const classroomId = document.getElementById("classroom-select").value;
    if (!classroomId || classroomId === "") {
        alert("请选择考试教室");
        return;
    }

    document.getElementById("confirm-subject").innerText = subject;
    document.getElementById("confirm-duration").innerText = duration + " 分钟";
    document.getElementById("confirm-classroom").innerText = classroom;

    const modal = document.getElementById("modal-start-confirm");
    if (modal) modal.classList.remove("hidden");
};

/**
 * 确认开始考试
 */
window.confirmStartExam = async function () {
    const subject = document.getElementById("subject-input").value;
    const duration = document.getElementById("exam-duration").value;
    const roomId = document.getElementById("classroom-select").value;

    try {
        const resp = await fetch(`/exam/start?token=${token}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                subject: subject,
                duration: parseInt(duration),
                classroom_id: parseInt(roomId),
            }),
        });
        if (resp.ok) {
            console.log("考试已启动");
            isRunning = true;
            localRemainingSeconds = parseInt(duration) * 60;
            connectStream();
            updateButtonStates();
        }
    } catch (e) {
        console.error("启动失败:", e);
    }
    window.closeModal("modal-start-confirm");
};

/**
 * 断开连接/结束考试
 */
window.disconnectStream = function () {
    const modal = document.getElementById("modal-stop-confirm");
    if (modal) modal.classList.remove("hidden");
};

/**
 * 确认断开
 */
window.confirmDisconnect = async function () {
    try {
        const resp = await fetch(`/exam/stop?token=${token}`, {
            method: "GET",
        });
        if (resp.ok) {
            console.log("考试已停止");
            isRunning = false;
            stopStream();
            updateButtonStates();
        }
    } catch (e) {
        console.error("停止失败:", e);
    }
    window.closeModal("modal-stop-confirm");
};

/**
 * 标定/重置追踪逻辑
 */
window.calibrate = window.resetTracks = async function () {
    if (!isRunning) return;
    if (!confirm("确定要重置当前所有的追踪锁定并重新标定吗？")) return;
    try {
        const resp = await fetch(`/exam/recalibrate?token=${token}`, {
            method: "POST",
        });
        if (resp.ok) {
            alert("标定指令已下发，请观察视频反馈。");
        }
    } catch (e) {
        console.error("标定请求失败:", e);
    }
};

/**
 * 通用关闭模态框
 */
window.closeModal = function (modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.add("hidden");
};

/**
 * 重置统计
 */
window.resetStats = async function () {
    if (!confirm("确定要清空当前的异常统计数据吗？")) return;
    try {
        const resp = await fetch(`/exam/anomalies/reset?token=${token}`, {
            method: "POST",
        });
        if (resp.ok) {
            alert("异常统计数据已重置。");
            // 清空异常图片和点
            updateCarousel([]);
            updatePoints([]);
        }
    } catch (e) {
        console.error("重置统计请求失败:", e);
    }
};

/**
 * 加载异常图片
 */
async function loadAnomalyImages(count) {
    try {
        const resp = await fetch(
            `/exam/anomalies/images?count=${count}&token=${token}`,
        );
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.success) {
            updateCarousel(data.images);
            updatePoints(data.images);
        }
    } catch (e) {
        console.error("加载异常图片失败:", e);
    }
}

/**
 * 更新底部轮播图片
 */
/**
 * 更新热力图逻辑 (使用 heatmap.js)
 */
async function updateHeatmap() {
    const container = document.querySelector("#card-right .canvas-container");
    const videoFeed = document.getElementById("video-feed");

    if (!container || !videoFeed || videoFeed.naturalWidth === 0) return;

    // 初始化 heatmapInstance
    if (!heatmapInstance && window.h337) {
        heatmapInstance = h337.create({
            container: container,
            radius: 40,
            maxOpacity: 0.6,
            minOpacity: 0,
            blur: 0.85,
            gradient: {
                "0.00": "rgb(22, 10, 30)",
                0.05: "rgb(57, 16, 80)",
                0.11: "rgb(92, 26, 104)",
                0.16: "rgb(132, 42, 120)",
                0.22: "rgb(176, 60, 105)",
                0.27: "rgb(215, 82, 82)",
                0.33: "rgb(236, 116, 52)",
                0.38: "rgb(247, 156, 45)",
                0.44: "rgb(252, 198, 96)",
                "0.50": "rgb(252, 237, 161)",
                0.55: "rgb(252, 237, 161)",
                0.61: "rgb(252, 237, 161)",
                0.66: "rgb(252, 237, 161)",
                0.72: "rgb(252, 237, 161)",
                0.77: "rgb(252, 237, 161)",
                0.83: "rgb(252, 237, 161)",
                0.88: "rgb(252, 237, 161)",
                0.94: "rgb(252, 237, 161)",
                "1.00": "rgb(252, 237, 161)",
            },
        });
    }

    if (!heatmapInstance) return;

    try {
        const resp = await fetch(`/exam/anomalies?token=${token}`);
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data.success) return;

        const scaleX = container.clientWidth / videoFeed.naturalWidth;
        const scaleY = container.clientHeight / videoFeed.naturalHeight;

        const points = data.anomalies
            .filter((item) => item.count > 0)
            .map((item) => {
                const coords = item.coord.replace(/[()]/g, "").split(",");
                return {
                    x: Math.round(parseFloat(coords[0]) * scaleX),
                    y: Math.round(parseFloat(coords[1]) * scaleY),
                    value: item.count,
                    // 频数越高，半径越大，使其更容易连成一片
                    radius: Math.min(100, 30 + Math.log2(item.count + 1) * 15),
                };
            });

        // 设置 heatmap 数据。每个档位频数加 50，20个档位即 max 值为 1000
        heatmapInstance.setData({
            max: 1000,
            data: points,
        });
    } catch (e) {
        console.error("更新热力图失败:", e);
    }
}

function updateCarousel(images) {
    const marqueeTrack = document.getElementById("marquee-track");
    if (!marqueeTrack) return;

    // 添加滚轮交互支持
    const scrollContainer = document.getElementById(
        "carousel-scroll-container",
    );
    if (scrollContainer && !scrollContainer.dataset.wheelInit) {
        scrollContainer.addEventListener(
            "wheel",
            (e) => {
                e.preventDefault();
                scrollContainer.scrollLeft += e.deltaY + e.deltaX;
            },
            { passive: false },
        );
        scrollContainer.dataset.wheelInit = "true";
    }

    marqueeTrack.innerHTML = "";

    // 动态调整滚动速度：确保无论图片多少，移动速度基本一致
    // 假设每张图片及其间距大约需要 3 秒的滚动时间
    const totalImages = images.length;
    const duration = Math.max(20, totalImages * 3);
    marqueeTrack.style.setProperty("--scroll-speed", `${duration}s`);

    images.forEach((img) => {
        const wrapper = document.createElement("div");
        wrapper.className = "carousel-item-wrapper";
        wrapper.style.flexShrink = "0";
        wrapper.style.marginRight = "15px";
        wrapper.style.height = "80%"; // 占据公示栏高度的80%
        wrapper.style.aspectRatio = "16/9"; // 保持常见视频比例
        wrapper.style.cursor = "pointer";
        wrapper.style.overflow = "hidden";
        wrapper.style.borderRadius = "6px";
        wrapper.style.border = "1px solid rgba(255,255,255,0.1)";

        const imgEl = document.createElement("img");
        imgEl.src = img.url;
        imgEl.className = "carousel-img";
        imgEl.style.width = "100%";
        imgEl.style.height = "100%";
        imgEl.style.objectFit = "cover";
        imgEl.onclick = () => window.openLightbox(img.url);

        // 鼠标悬停时在画面上标出十字光标
        wrapper.onmouseenter = () => {
            const videoFeed = document.getElementById("video-feed");
            const heatmapCanvas = document.getElementById("heatmap-canvas");

            if (videoFeed && videoFeed.naturalWidth > 0) {
                const scaleX = videoFeed.clientWidth / videoFeed.naturalWidth;
                const scaleY = videoFeed.clientHeight / videoFeed.naturalHeight;

                const markers = document.querySelectorAll(".focus-marker");
                markers.forEach((marker) => {
                    const container = marker.closest(".canvas-container");
                    if (container) {
                        const targetScaleX =
                            container.clientWidth / videoFeed.naturalWidth;
                        const targetScaleY =
                            container.clientHeight / videoFeed.naturalHeight;
                        marker.style.display = "block";
                        marker.style.left = img.x * targetScaleX + "px";
                        marker.style.top = img.y * targetScaleY + "px";
                    }
                });
            }
        };
        wrapper.onmouseleave = () => {
            const markers = document.querySelectorAll(".focus-marker");
            markers.forEach((marker) => {
                marker.style.display = "none";
            });
        };

        wrapper.appendChild(imgEl);
        marqueeTrack.appendChild(wrapper);
    });
}

/**
 * 打开灯箱展示大图
 */
window.openLightbox = (url) => {
    const lightbox = document.getElementById("lightbox");
    const lightboxImg = document.getElementById("lightbox-img");
    if (lightbox && lightboxImg) {
        lightboxImg.src = url;
        lightbox.style.display = "flex";
        document.body.classList.add("lightbox-open");
    }
};

/**
 * 关闭灯箱
 */
window.closeLightbox = () => {
    const lightbox = document.getElementById("lightbox");
    if (lightbox) {
        lightbox.style.display = "none";
        document.body.classList.remove("lightbox-open");
    }
};

/**
 * 更新画面上的异常点
 */
function updatePoints(images) {
    anomalyPoints = images.map((img) => ({ x: img.x, y: img.y }));
    const overlay = document.getElementById("video-overlay");
    if (!overlay) return;
    // 清空旧点
    overlay.querySelectorAll(".anomaly-point").forEach((e) => e.remove());
    // 添加新点
    anomalyPoints.forEach((p) => {
        const div = document.createElement("div");
        div.className = "anomaly-point";
        div.style.left = p.x + "px";
        div.style.top = p.y + "px";
        overlay.appendChild(div);
    });
}

// ================= 全屏控制逻辑 =================
window.toggleGlobalFullscreen = () => {
    if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen();
    } else {
        document.exitFullscreen();
    }
};

window.toggleLocalFullscreen = (id) => {
    const elem = document.getElementById(id);
    if (!document.fullscreenElement) {
        elem.requestFullscreen();
    } else {
        document.exitFullscreen();
    }
};
