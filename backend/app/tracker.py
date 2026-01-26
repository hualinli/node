import numpy as np
from scipy.optimize import linear_sum_assignment

def vectorized_iou(boxes1, boxes2):
    """
    向量化计算多个边界框的交并比
    boxes1: (N, 4) numpy array
    boxes2: (M, 4) numpy array
    返回: (N, M) IOU 矩阵
    """
    boxes1 = np.array(boxes1)
    boxes2 = np.array(boxes2)

    # 计算交集的坐标
    x1 = np.maximum(boxes1[:, 0:1], boxes2[:, 0:1].T)  # (N, M)
    y1 = np.maximum(boxes1[:, 1:2], boxes2[:, 1:2].T)
    x2 = np.minimum(boxes1[:, 2:3], boxes2[:, 2:3].T)
    y2 = np.minimum(boxes1[:, 3:4], boxes2[:, 3:4].T)

    inter_w = np.maximum(0, x2 - x1)
    inter_h = np.maximum(0, y2 - y1)
    inter_area = inter_w * inter_h

    # 计算并集面积
    area1 = (boxes1[:, 2] - boxes1[:, 0]) * (boxes1[:, 3] - boxes1[:, 1])  # (N,)
    area2 = (boxes2[:, 2] - boxes2[:, 0]) * (boxes2[:, 3] - boxes2[:, 1])  # (M,)
    union_area = area1[:, np.newaxis] + area2[np.newaxis, :] - inter_area

    iou = inter_area / union_area
    iou[union_area == 0] = 0
    return iou


class Track:
    def __init__(self, box, track_id):
        self.id = track_id
        self.boxes = [box]  # 存储历史框
        self.last_update = 0  # 最后更新帧数

    def update(self, box):
        self.boxes.append(box)
        self.last_update = 0

    def get_avg_center(self):
        if not self.boxes:
            return None
        centers = []
        for box in self.boxes:
            center_x = (box[0] + box[2]) / 2
            center_y = (box[1] + box[3]) / 2
            centers.append([center_x, center_y])
        avg_center = np.mean(centers, axis=0)
        return avg_center.astype(int).tolist()  # 返回列表格式 [x, y]


class Tracker:
    def __init__(self, max_age=10, iou_threshold=0.3):
        self.tracks = []  # 当前活跃的tracks
        self.next_id = 0
        self.max_age = max_age  # 最大丢失帧数
        self.iou_threshold = iou_threshold  # IOU匹配阈值

    def update(self, detections):
        """
        更新跟踪器
        detections: list of boxes [[x1, y1, x2, y2], ...]
        """
        if not self.tracks:
            # 初始帧，创建新tracks
            for det in detections:
                self.tracks.append(Track(det, self.next_id))
                self.next_id += 1
            return

        # 构建成本矩阵 (1 - IOU)
        if len(self.tracks) > 0 and len(detections) > 0:
            track_boxes = np.array([t.boxes[-1] for t in self.tracks])
            det_boxes = np.array(detections)
            iou_matrix = vectorized_iou(track_boxes, det_boxes)
            cost_matrix = 1 - iou_matrix
        else:
            cost_matrix = np.zeros((len(self.tracks), len(detections)))

        # 使用匈牙利算法进行匹配
        row_ind, col_ind = linear_sum_assignment(cost_matrix)

        matched_tracks = set()
        matched_dets = set()
        for r, c in zip(row_ind, col_ind):
            if cost_matrix[r, c] < 1 - self.iou_threshold:  # IOU > threshold
                self.tracks[r].update(detections[c])
                matched_tracks.add(r)
                matched_dets.add(c)

        # 未匹配的检测创建新track
        for j in range(len(detections)):
            if j not in matched_dets:
                self.tracks.append(Track(detections[j], self.next_id))
                self.next_id += 1

        # 未匹配的tracks增加age
        for i in range(len(self.tracks)):
            if i not in matched_tracks:
                self.tracks[i].last_update += 1

        # 删除超时的tracks
        self.tracks = [t for t in self.tracks if t.last_update <= self.max_age]

    def get_final_centers(self):
        """
        获取每个目标的最终坐标 (平均中心点)
        返回: dict {track_id: [x, y]}
        """
        return {t.id: t.get_avg_center() for t in self.tracks if t.get_avg_center() is not None}
