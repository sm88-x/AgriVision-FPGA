# C 接入说明

入口为 algorithm.seed_separation.SeedSeparator。species是调用者提供的品种：soybean/mungbean/redbean；只承诺固定白底和现有工作ROI条件。

```python

import json, cv2

from pathlib import Path

from algorithm.seed_separation import SeedSeparator

root=Path('.')

frozen=json.loads((root/'data/evidence/segmentation_comparison/frozen.json').read_text(encoding='utf-8'))

engine=SeedSeparator(frozen, root/'data/models/best.pt')

image=cv2.imread(str(root/'data/datasets/seeds/raw/soybean_pair_003.png'))

species='soybean'

prediction=engine.predict(image, frame_id=13, species=species)

results={mode:engine.detect_and_separate(image,mode,species,

    session_id='qt_comparison_001',frame_id=13,image_id='soybean_pair_003',

    gt_count=None,prediction=prediction)

    for mode in ('baseline_0','baseline_1','proposed_v1')}

```

模型只初始化一次，放在工作线程，避免阻塞Qt。相同session_id+frame_id+image_id才能叠图。objects[].id只在当前算法结果内编号，不代表三路间的同一粒匹配。bbox_xyxy_src和contour_xy_src已是原图像素坐标，禁止再次撤销letterbox；只做Qt显示缩放。

推荐先离线读取handoff/examples.json。image及modes路径均相对于该manifest所在handoff目录，可不带模型验证15图。三种种子各5张，后台CSV、JSON计数和帧身份已自动核对；Qt实际显示和10次连续操作仍需C验收，表中明确标PENDING_C。

gt_count/absolute_error为null时显示N/A。fpga_dma_ms为null，不能填以前的板端耗时。UNRESOLVED组保留原检测框，contour可为空。status=OK只表示算法接受，不保证与GT一致。使用CSV的绝对误差判断计数结果，另展示未解决标记。

框统计使用同一工作ROI；raw_detections仅在baseline_0 JSON保存全图原始检测，禁止把全图框数当作ROI内seed_count。每种品种三路共享同一真实推理：黄豆/红豆imgsz=960、iou=0.55，绿豆imgsz=960、iou=0.7；conf均0.25。参数仅在dev选定并冻结。

