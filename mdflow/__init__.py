# -*- coding: utf-8 -*-
"""MD Workflow Manager —— 分子动力学模拟流程管理软件。

输入 CHARMM-GUI（GROMACS 格式）输出目录，在界面配置参数后，
自动完成 能量最小化 → 平衡(NVT/NPT) → 生产模拟 全流程，
实时显示进度 / 剩余时间，并输出到指定目录。
"""

__version__ = "0.1.0"
