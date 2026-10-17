# -*- coding: utf-8 -*-
"""MD Workflow Manager 入口。

用法:
    python main.py                  启动图形界面
    python main.py --selftest       运行自检（无需 GROMACS）
    python main.py --cli <输入目录或压缩包> [选项]
                                    控制台模式（无 GUI，适合服务器/nohup/tmux）
                                    例: python main.py --cli /path/charmm-gui.tgz -o ~/MD_sim
                                    完整选项: python main.py --cli -h
"""
import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MD Workflow Manager - 分子动力学模拟流程管理软件")
    parser.add_argument("--selftest", action="store_true",
                        help="运行自检（无需 GROMACS）")
    parser.add_argument("--gui", action="store_true",
                        help="启动图形界面（默认行为）")
    parser.add_argument("--cli", nargs=argparse.REMAINDER,
                        help="控制台模式（之后的所有参数传给 CLI，--cli -h 查看帮助）")
    args = parser.parse_args()

    if args.selftest:
        from mdflow.selftest import run_selftest
        return run_selftest()

    if args.cli is not None:
        from mdflow.cli import main as cli_main
        return cli_main(args.cli)

    from mdflow.gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
