#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dma_test —— Generic DMA send/recv check for any PYNQ overlay.

适用范围
--------
验证任意兼容 Overlay 的 DMA 基本发送/接收链路,不绑定具体算法、图片或路径。
典型场景:带 AXI DMA(M_AXIS_MM2S 发送 / S_AXIS_S2MM 接收)的 Overlay,
内核按已知规则消费输入流并产生输出流(如硬件加速核)。

本 Skill 不 import 任何项目模块,可在任意 PYNQ 环境使用。

完成判定与顺序
------------------------------------------------------
- 完成判定: 轮询 AXI DMA 状态寄存器 IOC 位(MM2S_DMASR@0x04 / S2MM_DMASR@0x34,
  bit12, 写 1 清除), 带超时、不依赖中断; 取不到 DMA 基址时回退到 channel.wait()。
- 顺序: 清 IOC → 挂 recv → 挂 send → start 回调 → 轮询完成。
  "先挂 DMA 再 ap_start"是本板实测的稳定顺序(反序不稳定, 见 edge_morph_driver.py)。

输入
----
dma         : DMA 驱动对象,或 overlay.ip_dict 中的名称字符串(需同时传 overlay)
data        : 输入数组或测试数据(任意可 np.asarray 的整数序列/数组)
out_length  : 期望接收的拍数(字节数),正整数
reference   : 可选,期望输出(长度必须等于 out_length),用于逐元素比对
dtype       : 收发 buffer 的数据类型,默认 uint8(可为 uint16/uint32)
overlay     : 仅当 dma 为名称字符串时必填
timeout     : 完成等待超时秒数,默认 10
start       : 可选回调,在两个 DMA 通道挂好之后、等待完成之前执行。
              需要 ap_start 的内核(ap_ctrl_hs)把"写寄存器 + 拉 ap_start"放这里,
              以保证"先挂 DMA 再 ap_start"的稳定顺序。

输出
----
dict: status(PASS/FAIL)、output(收到的数据)、mismatches、max_diff、checksum 等。
同时向 stdout 打印一行式 PASS/FAIL 报告;失败时附排查提示。

失败条件与提示
--------------
DMA未找到 / 长度不匹配 / wait 卡住 / TLAST·输出长度异常 —— 详见 README。
"""
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src/software").is_dir())
for _layout_rel in ("src/software", "src/software/app", "src/software/adapters",
                    "src/software/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

import sys
import time

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows GBK 控制台防乱码
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from pynq import MMIO, allocate
except ImportError:
    MMIO = allocate = None

# AXI DMA 状态寄存器(所有 AXI DMA IP 布局相同)
MM2S_DMASR = 0x04
S2MM_DMASR = 0x34
DMASR_IOC = 0x1000        # bit12 传输完成(写 1 清除)
POLL_INTERVAL = 0.0001    # 轮询间隔 0.1 ms,越小计时越准

_SIDE_LABEL = {
    "send": "发送端(MM2S)",
    "recv": "接收端(S2MM)",
    "both": "发送端(MM2S)与接收端(S2MM)",
}

def _fail(reason):
    return {"status": "FAIL", "reason": reason}

def _finish_fail(reason):
    print("[dma_test] FAIL: %s" % reason)
    return _fail(reason)

def _resolve_dma(dma, overlay):
    """把 DMA 对象或名称解析为 DMA 驱动对象。返回 (dma_obj, err)。"""
    if dma is None:
        return None, "DMA未找到: dma 参数为 None。"
    if isinstance(dma, str):
        if overlay is None:
            return None, (
                "DMA未找到: 传入的是名称字符串 '%s',"
                " 需要同时传入 overlay 参数(名称经 overlay.ip_dict 解析)。"
                % dma
            )
        # PYNQ 2.7+: getattr(overlay, name) 返回驱动对象, ip_dict 值是描述 dict;
        # 旧版 PYNQ 的 ip_dict 值可能直接是驱动对象, 两者都试。
        obj = getattr(overlay, dma, None)
        if not (obj is not None and hasattr(obj, "sendchannel")
                and hasattr(obj, "recvchannel")):
            alt = overlay.ip_dict.get(dma)
            if alt is not None and hasattr(alt, "sendchannel") \
                    and hasattr(alt, "recvchannel"):
                obj = alt
        if obj is None or not (hasattr(obj, "sendchannel")
                               and hasattr(obj, "recvchannel")):
            names = sorted(overlay.ip_dict.keys())
            return None, (
                "DMA未找到: '%s' 不在 overlay 中或不是 DMA 驱动。"
                " overlay 中全部设备: %s" % (dma, names)
            )
        return obj, None
    # 直接传入对象:检查是否像 DMA 驱动
    if not (hasattr(dma, "sendchannel") and hasattr(dma, "recvchannel")):
        return None, (
            "DMA未找到: 传入对象没有 sendchannel/recvchannel,"
            " 不是 PYNQ DMA 驱动。"
        )
    return dma, None

def _get_dma_mmio(dma, overlay, dma_obj):
    """取 DMA 基址对应的 MMIO,用于 IOC 轮询。返回 (mmio, err)。"""
    if MMIO is None:
        return None, "环境不支持 MMIO, 完成判定回退到 channel.wait()"
    if isinstance(dma, str) and overlay is not None:
        info = overlay.ip_dict.get(dma)
        if isinstance(info, dict) and "phys_addr" in info:
            try:
                return MMIO(info["phys_addr"], 0x1000), None
            except Exception as e:
                return None, "MMIO 打开失败(%s), 回退到 channel.wait()" % e
    if hasattr(dma_obj, "mmio"):
        return dma_obj.mmio, None
    return None, "取不到 DMA 基址, 回退到 channel.wait()(无超时保护)"

def _poll_done(mmio, timeout):
    """轮询两个通道 IOC 位直到都置起或超时。

    返回 (err, side): 成功 err=None;超时时 side 指示卡在哪一侧,
    用于选择打印哪组排查提示。
    """
    t0 = time.perf_counter()
    while True:
        m = mmio.read(MM2S_DMASR)
        s = mmio.read(S2MM_DMASR)
        m_done = bool(m & DMASR_IOC)
        s_done = bool(s & DMASR_IOC)
        if m_done and s_done:
            return None, None
        if time.perf_counter() - t0 > timeout:
            if not m_done and not s_done:
                side = "both"
            elif not m_done:
                side = "send"
            else:
                side = "recv"
            return ("wait 卡住: %s %s 秒内未完成"
                    "(MM2S_DMASR=0x%x, S2MM_DMASR=0x%x, IOC 未置位)"
                    % (_SIDE_LABEL[side], timeout, m, s)), side
        time.sleep(POLL_INTERVAL)

def _wait_plain(channel, timeout, which):
    """无 MMIO 时的回退: 用 PYNQ channel.wait()。

    支持 timeout 参数则用;不支持(TypeError)退化为阻塞等待(卡住时需人工中断)。
    """
    try:
        channel.wait(timeout=timeout)
        return None
    except TypeError:
        channel.wait()
        return None
    except (RuntimeError, TimeoutError) as e:
        return "wait 卡住: %s 通道 %s 秒内未完成(%s)" % (which, timeout, e)

def _hints_send_stuck():
    """发送端卡住 = 内核未消费输入。"""
    return [
        "[dma_test] 排查顺序(发送端卡住 = 内核未消费输入):",
        "[dma_test]   1. start 回调是否真的拉起了 ap_start(ap_ctrl_hs 每帧都要重新拉一次)",
        "[dma_test]   2. 顺序: 先挂 DMA 再 ap_start(本板实测反序不稳定)",
        "[dma_test]   3. width/height 等参数寄存器是否已写、是否在合法范围",
    ]

def _hints_recv_stuck():
    """接收端卡住 = 内核未产出足够数据(TLAST/输出长度异常)。"""
    return [
        "[dma_test] 排查顺序(接收端卡住 = 内核未产出足够数据):",
        "[dma_test]   1. 输入/输出长度是否与内核期望一致(= width x height 字节)",
        "[dma_test]   2. TLAST 是否只在帧尾出现一次(提前/缺失都让 S2MM 等不满)",
        "[dma_test]   3. 内核是否已启动(ap_start)且已完成(ap_done)",
        "[dma_test]   4. DMA 基址与内核 s_axi 基址是否与 hwh 一致(用 ol.ip_dict 动态读, 不要写死)",
        "[dma_test]   5. 卡住恢复: ol.reset() 重载 overlay 后再排查, 避免残留状态干扰",
    ]

def dma_test(dma, data, out_length, reference=None, dtype=np.uint8,
             overlay=None, timeout=10, start=None):
    """验证 DMA 发送/接收链路。详见模块 docstring 与 README。

    返回 dict:
      status      'PASS' / 'FAIL'
      reason      失败原因(PASS 时为 None)
      dma         DMA 名称
      dma_base    DMA 基址(hex 字符串;取不到为 None)
      send_length 实际发送拍数
      out_length  期望接收拍数
      output      收到的数据(numpy 数组,已从 buffer 拷贝)
      mismatches  与 reference 不一致的元素数(未传 reference 时为 None)
      max_diff    与 reference 的最大差(未传 reference 时为 None)
      checksum    输出数据 uint64 和
    """
    if allocate is None:
        return _finish_fail("环境错误: import pynq 失败, 请在 PYNQ 环境中运行。")

    # ---- 参数检查 ----
    try:
        out_length = int(out_length)
    except (TypeError, ValueError):
        return _finish_fail("参数错误: out_length 必须为正整数(期望接收拍数)。")
    if out_length <= 0:
        return _finish_fail("参数错误: out_length 必须为正整数(期望接收拍数)。")

    dma_obj, err = _resolve_dma(dma, overlay)
    if err:
        return _finish_fail(err)

    # ---- 输入归一化 ----
    arr = np.asarray(data)
    if arr.size == 0:
        return _finish_fail("参数错误: data 是空数组。")
    if not np.issubdtype(arr.dtype, np.integer):
        print("[dma_test] WARN: data dtype=%s 非整数, 将按 %s 截断。"
              % (arr.dtype, np.dtype(dtype).name))
    arr = np.asarray(arr, dtype=dtype).reshape(-1)
    n = arr.size
    if n > out_length:
        return _finish_fail(
            "长度不匹配: 输入 %d 拍 > 输出缓冲 %d 拍,"
            " 请核对 out_length 与数据长度。" % (n, out_length))

    # ---- reference 预检(先于任何 DMA 操作,快速失败)----
    ref = None
    if reference is not None:
        ref = np.asarray(reference, dtype=dtype).reshape(-1)
        if ref.size != out_length:
            return _finish_fail(
                "长度不匹配: reference %d 拍 != out_length %d 拍, 无法比较。"
                % (ref.size, out_length))

    dma_name = dma if isinstance(dma, str) \
        else getattr(dma_obj, "name", type(dma_obj).__name__)
    mmio, mmio_err = _get_dma_mmio(dma, overlay, dma_obj)
    if mmio_err:
        print("[dma_test] WARN: %s" % mmio_err)

    # ---- buffer 分配 -> 清 IOC -> 挂 recv/send -> start -> 等完成 ----
    in_buf = allocate(shape=(n,), dtype=np.dtype(dtype))
    out_buf = allocate(shape=(out_length,), dtype=np.dtype(dtype))
    try:
        np.copyto(in_buf, arr)
        if mmio is not None:
            # 写 1 清除上一帧完成标志,避免把上一帧的完成误判成本帧
            mmio.write(MM2S_DMASR, DMASR_IOC)
            mmio.write(S2MM_DMASR, DMASR_IOC)
        try:
            dma_obj.recvchannel.transfer(out_buf)   # 先挂接收,让接收侧提前就位
            dma_obj.sendchannel.transfer(in_buf)    # 再挂发送
        except Exception as e:
            return _finish_fail("DMA transfer 启动失败: %s" % e)

        if start is not None:
            try:
                start()
            except Exception as e:
                return _finish_fail("start 回调执行失败: %s" % e)

        if mmio is not None:
            err, side = _poll_done(mmio, timeout)
            if err:
                print("[dma_test] FAIL: %s" % err)
                if side in ("send", "both"):
                    for line in _hints_send_stuck():
                        print(line)
                if side in ("recv", "both"):
                    for line in _hints_recv_stuck():
                        print(line)
                return _fail(err)
        else:
            err = _wait_plain(dma_obj.sendchannel, timeout, "MM2S(发送)")
            if err is None:
                err = _wait_plain(dma_obj.recvchannel, timeout, "S2MM(接收)")
            if err:
                print("[dma_test] FAIL: %s" % err)
                for line in _hints_recv_stuck():
                    print(line)
                return _fail(err)

        # 从 buffer 拷贝到普通 numpy 数组(数据返回给调用者)
        out = np.empty(out_length, dtype=np.dtype(dtype))
        np.copyto(out, out_buf)
    finally:
        for b in (in_buf, out_buf):
            try:
                b.freebuffer()
            except AttributeError:
                pass

    # ---- 比较 ----
    checksum = int(out.astype(np.uint64).sum())
    mismatches = None
    max_diff = None
    if ref is not None:
        mismatches = int(np.count_nonzero(out != ref))
        max_diff = int(np.abs(out.astype(np.int64)
                              - ref.astype(np.int64)).max()) \
            if mismatches else 0

    # ---- 报告 ----
    dma_base = None
    if mmio is not None:
        dma_base = hex(getattr(mmio, "base_addr",
                               getattr(mmio, "base", 0)))
    elif isinstance(dma, str) and overlay is not None:
        info = overlay.ip_dict.get(dma)
        if isinstance(info, dict) and "phys_addr" in info:
            dma_base = hex(info["phys_addr"])

    pad_note = "" if n == out_length else "(输入不足部分补零)"
    lines = [
        "[dma_test] DMA: %s%s"
        % (dma_name, " @ " + dma_base if dma_base else ""),
        "[dma_test] send %d x %s -> recv %d x %s %s"
        % (n, np.dtype(dtype).name, out_length, np.dtype(dtype).name, pad_note),
    ]
    if ref is not None:
        lines.append("[dma_test] compare: %d mismatch(es), max diff = %d"
                     % (mismatches, max_diff))
    else:
        lines.append("[dma_test] 无 reference: 仅验证链路, 不验证内容; checksum = %d"
                     % checksum)
    lines.append("[dma_test] 前 8 个输出值: %s" % out[:8].tolist())
    status = "PASS" if (ref is None or mismatches == 0) else "FAIL"
    lines.append("[dma_test] %s" % status)
    for line in lines:
        print(line)

    return {
        "status": status,
        "reason": None if status == "PASS" else
                  "%d 个元素与 reference 不一致, 最大差 %d"
                  % (mismatches, max_diff),
        "dma": dma_name,
        "dma_base": dma_base,
        "send_length": n,
        "out_length": out_length,
        "output": out,
        "mismatches": mismatches,
        "max_diff": max_diff,
        "checksum": checksum,
    }

if __name__ == "__main__":
    print("dma_test 需要在 PYNQ 环境中使用, 参见 skill/dma_test/README.md。")
