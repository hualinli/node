import cv2
import numpy as np
from mindx.sdk import Tensor, base


class MindXModel:
    """
    封装 MindX 模型推理类
    """
    def __init__(self, model_path, device_id):
        self.model = base.model(modelPath=model_path, deviceId=device_id)

    def infer(self, data):
        input_tensor = Tensor(np.ascontiguousarray(data))
        return [np.array(out) for out in self.model.infer([input_tensor])]

def post_process_det(pred, orig_wh, conf_thres, iou_thres, det_size):
    """
    detect 后处理。
    arg：
        pred：模型输出的预测结果。shape:(1, 4+num_classes, num_boxes)
        orig_wh：原始图像的宽高。shape:(2,)
        conf_thres：置信度阈值。float
        iou_thres：IOU阈值。float
        det_size：检测框的大小。shape:(2,)
    return：
        final_boxes：检测框的坐标。shape：(N, 4)
        scores：检测框的置信度。shape：(N,)
    """
    # 转置预测结果
    pred = np.squeeze(pred).T
    # 计算每个框的最大置信度
    scores = pred[:, 4:].max(axis=1)
    # 过滤低置信度框
    mask = scores > conf_thres
    pred, scores = pred[mask], scores[mask]

    if len(pred) == 0:
        return [], []

    # 转换坐标格式: [center_x, center_y, w, h] -> [x, y, w, h]
    boxes = np.empty_like(pred[:, :4])
    boxes[:, 0] = pred[:, 0] - pred[:, 2] / 2
    boxes[:, 1] = pred[:, 1] - pred[:, 3] / 2
    boxes[:, 2], boxes[:, 3] = pred[:, 2], pred[:, 3]

    # 执行非极大值抑制
    keep = cv2.dnn.NMSBoxes(boxes, scores, conf_thres, iou_thres)
    if len(keep) == 0:
        return [], []
    keep = keep.flatten()

    # 缩放坐标回原始图像尺寸
    w_s, h_s = orig_wh[0] / det_size[0], orig_wh[1] / det_size[1]
    final_boxes = np.empty((len(keep), 4), dtype=np.int32)
    final_boxes[:, 0] = boxes[keep, 0] * w_s
    final_boxes[:, 1] = boxes[keep, 1] * h_s
    final_boxes[:, 2] = (boxes[keep, 0] + boxes[keep, 2]) * w_s
    final_boxes[:, 3] = (boxes[keep, 1] + boxes[keep, 3]) * h_s

    return final_boxes, scores[keep]
