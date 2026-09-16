1.确定 模型推理时的调用代码链是根据  model\trueno3_src-master-0.0.10\storage\model_descriptor.yaml 里内容来确定，而不是项目自定义，方便以后切换
model\trueno3_src-master-0.0.10\src\algorithm或者model\trueno3_src-master-0.0.10\src\base 调用代码内容时可以直接套用。

例如model_descriptor.yaml：

    light_pgdzsd_mn:
        desc: mn屏柜大指示灯状态
        pipeline:
            1:
                base: base_yolo
                cls_names: *pg_cls_names
                param: *pg_param 
                patch: light_pgzsd
        select_cls:
            - dzsd_m 
            - dzsd_red
            - dzsd_green
            - dzsd_yellow
            - dzsd_white
            - dzsd_blue
            - dzsd_other
        trans_cls:
            dzsd_blue: 亮蓝灯
            dzsd_green: 亮绿灯
            dzsd_m: 指示灯灭
            dzsd_other: 指示灯亮
            dzsd_red: 亮红灯
            dzsd_white: 亮白灯
            dzsd_yellow: 亮黄灯


那么对于为 light_pgdzsd_mn （调用 at-det-pg.m类模型）时，分类标签为：
“
        select_cls:
            - dzsd_m
            - dzsd_red
            - dzsd_green
            - dzsd_yellow
            - dzsd_white
            - dzsd_blue
            - dzsd_other
”
trans_cls是翻译，也是最终在推理图上显示得推理结果

base: base_yolo ==> 那么去model\trueno3_src-master-0.0.10\src\base又或model\trueno3_src-master-0.0.10\src\algorithm\base 找到对应代码（pt类模型是base_yolo_nv或者base_ocr_nv、base_ppocr_nv或者其他，你来确定）
patch: light_pgzsd ==>即 model\trueno3_src-master-0.0.10\src\algorithm里文件名名，即可，要做到使用改脚本来调用模型，因为有些模型是套用这些逻辑实现功能的，比如 数字显示 有些站点需要 去除前几个数字，得通过这些调用代码来处理，不走这套调用没法验证是否代码有效

2.ROI功能改变为标定框功能，比如：
{'switch_pgbhyb_mn': {'roi': [], 'on_intersection_output': [{'type': 'polygon', 'points': [652, 408, 2066, 323, 2055, 989, 836, 975], 'usage': 'analyze'}], 'marks': [], 'needle': [], 'extra_data': {'value_map': {'0': '识别为空', '1': '压板分', '2': '压板合', '3': '无压板'}, 'extra_data': '1'}}}

本项目需要标定框来辅助模型推理，而不是需要ROI功能，具体意指可以通过代码去理解学习。
ROI右键点击改为结束改为 右键点击闭合最后一个点得到一个内空得大于等于四得多边形，
点击 “标定”按钮后，
左单击 ==>一个点
左单击 ==>连接上一个点，得一条线
左单击 ==>连接上两个点，得三边形
右键单击 ==>连接最新上两个点，得四边形，并结束


3.设置版本文件 18.algorithm-web\Document\VERSION
每次迭代后自增，并简单说明添加了什么功能，解决什么问题。

之前未读取过rust_upload_backed_and_fonrted_use_test-tag-0912/Document/AGENTS.md优先读取，用于明确职责，有的话不再读取。

