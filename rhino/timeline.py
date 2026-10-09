#! python3
"""Rhino 8 非模态施工时间轴；读取模型日期，复用 replay_install 的原生状态。"""
import os
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta

ROOT = os.environ.get("FACADE_BIM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "rhino"))

import Eto.Drawing as ED
import Eto.Forms as EF
import Rhino
import scriptcontext as sc
import replay_install as replay

STICKY = "facade-bim.timeline"


class Timeline(EF.Form):
    def __init__(self, doc):
        super().__init__()
        self.doc = doc
        self.doc_serial = doc.RuntimeSerialNumber
        self.objects = replay.panel_objects(doc)
        if not self.objects:
            raise ValueError("请先打开含板块安装属性的 facade_bim.3dm")
        days = [date.fromisoformat(o.Attributes.GetUserString(k))
                for o in self.objects for k in ("delivery_date", "install_date")]
        current = date.fromisoformat(doc.Strings.GetValue("replay_date") or min(
            o.Attributes.GetUserString("install_date") for o in self.objects))
        self.first = min(min(days) - timedelta(days=1), current)
        self.last = max(max(days) + timedelta(days=1), current)
        self.playing = self.busy = self.closed = False
        self.applied = 0
        self.state = None
        self.Title = "幕墙 BIM · 施工时间轴"
        self.ClientSize = ED.Size(540, 590)
        self.Padding = ED.Padding(12)
        self.Owner = Rhino.UI.RhinoEtoApp.MainWindowForDocument(doc)
        self.slider = EF.Slider()
        self.slider.MinValue = 0
        self.slider.MaxValue = (self.last - self.first).days
        self.input = EF.TextBox()
        self.status = EF.Label()
        self.status.Wrap = EF.WrapMode.Word
        self.summary = EF.Label()
        self.summary.Wrap = EF.WrapMode.Word
        self.query = EF.TextBox()
        self.query.PlaceholderText = "板块编号，例如 S-L1-01；留空查询当前选择"
        self.details = EF.TextArea()
        self.details.ReadOnly = True
        self.details.Height = 245
        self.play_button = self.button("播放", self.toggle_play)
        self.timer = EF.UITimer()
        self.timer.Interval = 0.7
        self.timer.Elapsed += self.tick
        layout = EF.DynamicLayout()
        layout.Spacing = ED.Size(6, 8)
        layout.AddRow(self.label("日期范围：%s → %s" % (self.first, self.last)))
        layout.AddRow(self.slider)
        layout.AddRow(self.input, self.button("跳转", self.jump))
        layout.AddRow(self.button("前一天", lambda s, e: self.step(-1)), self.play_button,
                      self.button("后一天", lambda s, e: self.step(1)), self.button("导出当前", self.save))
        layout.AddRow(self.summary)
        layout.AddRow(self.label("当日安装按施工期间显示；运输架装空后次日释放。"))
        layout.AddRow(self.query, self.button("查询 / 选中", self.inspect))
        layout.AddRow(self.details)
        layout.AddRow(self.status)
        self.Content = layout
        self.slider.ValueChanged += self.changed
        self.Closed += self.on_closed
        Rhino.RhinoDoc.CloseDocument += self.on_document_closed
        self.set_day(current)
        if self.state is None:
            self.changed(None, None)

    @staticmethod
    def button(text, handler):
        button = EF.Button()
        button.Text = text
        button.Click += handler
        return button

    @staticmethod
    def label(text):
        label = EF.Label()
        label.Text = text
        return label

    @property
    def day(self):
        return self.first + timedelta(days=self.slider.Value)

    def set_day(self, value):
        if not self.first <= value <= self.last:
            raise ValueError("日期超出模型范围：%s 至 %s" % (self.first, self.last))
        self.slider.Value = (value - self.first).days

    def changed(self, sender, event):
        if self.busy or self.closed:
            return
        while True:
            if not self.available():
                return
            requested = self.day
            self.busy = True
            self.slider.Enabled = False
            undo = self.doc.BeginUndoRecord("幕墙施工日期 " + requested.isoformat())
            failed = False
            try:
                self.state = replay.apply(self.doc, self.objects, requested)
                if self.closed:
                    return
                self.applied += 1
                self.input.Text = requested.isoformat()
                counts = self.state["counts"]
                self.summary.Text = ("%s\n已安装 %d · 当日安装 %d · 在场待装 %d · 未到场 %d\n运输架 %d / %d" % (
                    requested, counts["installed"], counts["installing"], counts["on_site"],
                    counts["not_delivered"], self.state["occupied_slots"], self.state["slot_capacity"]))
                self.status.Text = "可继续旋转、选择和查询 Rhino 模型。"
            except Exception as exc:
                failed = True
                if not self.closed:
                    self.pause()
                    self.status.Text = str(exc)
            finally:
                if Rhino.RhinoDoc.FromRuntimeSerialNumber(self.doc_serial):
                    self.doc.EndUndoRecord(undo)
                self.busy = False
                if not self.closed:
                    self.slider.Enabled = True
            if failed or self.day == requested:
                break

    def step(self, amount):
        if self.busy or not self.available():
            return
        self.slider.Value = max(self.slider.MinValue, min(self.slider.MaxValue, self.slider.Value + amount))

    def jump(self, sender, event):
        if self.busy or not self.available():
            return
        try:
            self.set_day(date.fromisoformat(self.input.Text.strip()))
        except ValueError as exc:
            self.status.Text = str(exc)

    def pause(self):
        self.timer.Stop()
        self.playing = False
        self.play_button.Text = "播放"

    def toggle_play(self, sender=None, event=None):
        if self.busy or not self.available():
            return
        if self.playing:
            self.pause()
        elif self.slider.Value < self.slider.MaxValue:
            self.playing = True
            self.play_button.Text = "暂停"
            self.timer.Start()

    def tick(self, sender=None, event=None):
        if not self.playing or self.busy or not self.available():
            return
        if self.slider.Value >= self.slider.MaxValue:
            self.pause()
        else:
            self.step(1)
            if self.slider.Value == self.slider.MaxValue:
                self.pause()

    def inspect(self, sender=None, event=None):
        if self.busy or not self.available():
            return None
        value = (self.query.Text or "").strip()
        objects = [self.doc.Objects.FindId(o.Id) for o in self.objects]
        found = next((o for o in objects if o and o.Attributes.GetUserString("pid") == value), None)
        if not value:
            found = next((o for o in objects if o and o.IsSelected(False)), None)
        if not found:
            self.details.Text = "未找到板块。请输入完整编号或在模型中选择一个板块。"
            return None
        attrs = found.Attributes
        strings = attrs.GetUserStrings()
        self.details.Text = "\n".join("%s: %s" % (key, strings.Get(key)) for key in sorted(strings.AllKeys))
        self.doc.Objects.UnselectAll()
        if not found.IsHidden:
            found.Select(True)
            self.doc.Views.Redraw()
        self.status.Text = "板块 %s%s" % (attrs.GetUserString("pid"), "（当前日期尚未安装，几何隐藏）" if found.IsHidden else " 已选中")
        return attrs.GetUserString("pid")

    def save(self, sender=None, event=None):
        if self.closed:
            return False
        if self.busy:
            self.status.Text = "正在更新模型，请更新完成后导出。"
            return False
        if not self.available():
            return False
        if self.state is None:
            self.status.Text = "当前日期尚未成功更新，无法导出。"
            return False
        self.pause()
        self.busy = True
        self.slider.Enabled = False
        succeeded = False
        try:
            replay.export(self.doc, self.state)
            if not self.closed:
                self.status.Text = "已导出 model/replay/facade_%s.{3dm,png,json}" % self.state["date"]
            succeeded = True
        except Exception as exc:
            if not self.closed:
                self.status.Text = "导出失败：" + str(exc)
        finally:
            self.busy = False
            if not self.closed:
                self.slider.Enabled = True
        if self.available() and self.day.isoformat() != self.state["date"]:
            self.changed(None, None)
        return succeeded

    def available(self):
        if self.closed:
            return False
        if Rhino.RhinoDoc.FromRuntimeSerialNumber(self.doc_serial) is None:
            self.Close()
            return False
        active = Rhino.RhinoDoc.ActiveDoc
        if active is None or active.RuntimeSerialNumber != self.doc_serial:
            self.pause()
            self.status.Text = "已切换模型；请回到原模型后操作，或关闭后重新打开面板。"
            return False
        return True

    def on_document_closed(self, sender, event):
        if event.DocumentSerialNumber == self.doc_serial:
            self.Close()

    def on_closed(self, sender, event):
        self.pause()
        self.closed = True
        Rhino.RhinoDoc.CloseDocument -= self.on_document_closed
        if sc.sticky.get(STICKY) is self:
            del sc.sticky[STICKY]


def show(doc=None):
    doc = doc or Rhino.RhinoDoc.ActiveDoc
    if doc is None:
        raise ValueError("请先打开幕墙模型")
    old = sc.sticky.get(STICKY)
    if old and not old.closed:
        if old.doc_serial == doc.RuntimeSerialNumber:
            old.BringToFront()
            return old
        old.Close()
    form = Timeline(doc)
    sc.sticky[STICKY] = form
    form.Show()
    return form


def run_qa(doc):
    """原生创建真实 Eto 控件，触发事件并泵送实际播放定时器。"""
    form = show(doc)
    before = form.applied
    form.slider.Value = form.slider.MinValue
    assert form.state["visible_panels"] == 0
    form.slider.Value = form.slider.MaxValue
    assert form.state["visible_panels"] == len(form.objects)
    form.step(-1)
    assert form.state["date"] == (form.last - timedelta(days=1)).isoformat()
    form.input.Text = "2026-11-23"
    form.jump(None, None)
    assert form.state["date"] == "2026-11-23"
    original_apply = replay.apply
    interrupted = [False]
    def changing_apply(active_doc, objects, day):
        if not interrupted[0]:
            interrupted[0] = True
            assert form.save() is False, "更新中不得导出旧统计与新几何的混合快照"
            form.slider.Value += 1  # Actual ValueChanged event while an earlier update is busy.
        return original_apply(active_doc, objects, day)
    try:
        replay.apply = changing_apply
        form.step(-1)
    finally:
        replay.apply = original_apply
    assert interrupted[0] and form.state["date"] == form.day.isoformat() == form.input.Text
    form.set_day(date(2026, 11, 23))
    original_export = replay.export
    export_date = form.state["date"]
    def interrupted_export(active_doc, state):
        form.slider.Value += 1
        assert form.save() is False
        assert state["date"] == export_date == active_doc.Strings.GetValue("replay_date")
        original_export(active_doc, state)
        assert state["date"] == export_date == active_doc.Strings.GetValue("replay_date")
    try:
        replay.export = interrupted_export
        assert form.save() is True
    finally:
        replay.export = original_export
    assert form.state["date"] == form.day.isoformat() == form.input.Text and form.state["date"] != export_date
    form.query.Text = "S-L1-01"
    assert form.inspect() == "S-L1-01" and "install_date:" in form.details.Text
    old_day = form.day
    form.input.Text = "invalid-date"
    form.jump(None, None)
    assert form.day == old_day
    form.timer.Interval = 0.15
    form.toggle_play()
    target = form.slider.Value
    deadline = time.monotonic() + 8
    while form.slider.Value == target and time.monotonic() < deadline:
        Rhino.RhinoApp.Wait()
        time.sleep(0.02)
    assert form.slider.Value > target, "实际 Eto 定时器未推进日期"
    form.pause()
    paused = form.slider.Value
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        Rhino.RhinoApp.Wait()
        time.sleep(0.02)
    assert form.slider.Value == paused and not form.playing
    form.slider.Value = form.slider.MaxValue - 1
    form.toggle_play()
    form.tick()
    assert form.slider.Value == form.slider.MaxValue and not form.playing
    form.Close()
    assert form.closed and STICKY not in sc.sticky
    return {"ok": True, "real_eto_timer": True, "date_events": form.applied - before,
            "query_pid": "S-L1-01", "reverse_seek": True, "pause": True,
            "invalid_input_preserves_date": True, "latest_seek_wins": True, "busy_save_guarded": True,
            "export_reentry_guarded": True,
            "end_stops_playback": True, "close_stops_timer": True}


def run_lifecycle_qa(doc):
    form = show(doc)
    form.slider.Value = form.slider.MinValue
    form.toggle_play()
    assert form.playing
    doc.Modified = False
    # Opening the current path can be a no-op in Rhino. A distinct copy must
    # actually replace the active document and exercise its close event.
    with tempfile.TemporaryDirectory(prefix="facade_lifecycle_") as folder:
        copied = os.path.join(folder, "lifecycle.3dm")
        source = os.path.join(ROOT, "model", "facade_bim.3dm")
        shutil.copyfile(source, copied)
        try:
            assert Rhino.RhinoDoc.OpenFile(copied)
            deadline = time.monotonic() + 2
            while not form.closed and time.monotonic() < deadline:
                Rhino.RhinoApp.Wait()
                time.sleep(0.02)
            active = Rhino.RhinoDoc.ActiveDoc
            assert form.closed and not form.playing and STICKY not in sc.sticky, (
                "close lifecycle: old=%s new=%s alive=%s closed=%s playing=%s sticky=%s" % (
                    form.doc_serial, active.RuntimeSerialNumber if active else None,
                    Rhino.RhinoDoc.FromRuntimeSerialNumber(form.doc_serial) is not None,
                    form.closed, form.playing, STICKY in sc.sticky))
            assert form.save() is False and form.inspect() is None
            paused = form.slider.Value
            form.tick()
            assert form.slider.Value == paused
            fresh = show(Rhino.RhinoDoc.ActiveDoc)
            assert fresh is not form and fresh.available()
            fresh.Close()
        finally:
            active = Rhino.RhinoDoc.ActiveDoc
            if active:
                active.Modified = False
            # Release Windows' open 3dm handle before TemporaryDirectory cleanup.
            assert Rhino.RhinoDoc.OpenFile(source)
    return True


if __name__ == "__main__":
    show()
