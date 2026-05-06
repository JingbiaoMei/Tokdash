from __future__ import annotations

import argparse
import json
from datetime import datetime
from datetime import timedelta
from typing import Any

from .compute import compute_usage_with_comparison


PERIODS = {
    "Today": "today",
    "Last 7 Days": "week",
    "This Month": "month",
}


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _float_value(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def format_tokens(value: Any) -> str:
    number = _int_value(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:,}"


def format_money(value: Any) -> str:
    return f"${_float_value(value):,.2f}"


def format_delta(value: Any) -> str:
    if value is None:
        return "No previous data"
    number = _float_value(value)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.1f}% vs previous"


def _date_range_for_period(period: str) -> tuple[str, str, str]:
    today = datetime.now().astimezone().date()
    if period == "week":
        start = today - timedelta(days=6)
        return start.isoformat(), today.isoformat(), "Last 7 days"
    if period == "month":
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat(), "This month"
    return today.isoformat(), today.isoformat(), "Today"


def compute_native_dashboard_data(period: str) -> dict[str, Any]:
    date_from, date_to, range_label = _date_range_for_period(period)
    data = compute_usage_with_comparison(period, date_from, date_to)
    view_model = build_dashboard_view_model(data)
    view_model["date_from"] = date_from
    view_model["date_to"] = date_to
    view_model["range_label"] = range_label
    return view_model


def build_dashboard_view_model(data: dict[str, Any]) -> dict[str, Any]:
    models = data.get("combined_models") or data.get("top_models") or []
    apps = data.get("apps") or {}
    comparison = data.get("comparison") or {}
    timestamp = data.get("timestamp")
    refreshed = "Just now"
    if timestamp:
        try:
            refreshed = datetime.fromisoformat(timestamp).strftime("%H:%M")
        except ValueError:
            refreshed = str(timestamp)

    return {
        "period": data.get("period", "today"),
        "refreshed": refreshed,
        "metrics": [
            {
                "label": "Total tokens",
                "value": format_tokens(data.get("total_tokens")),
                "delta": format_delta(comparison.get("tokens_pct")),
            },
            {
                "label": "Estimated cost",
                "value": format_money(data.get("total_cost")),
                "delta": format_delta(comparison.get("cost_pct")),
            },
            {
                "label": "Messages",
                "value": f"{_int_value(data.get('total_messages')):,}",
                "delta": format_delta(comparison.get("messages_pct")),
            },
        ],
        "breakdown": [
            {
                "name": name,
                "tokens": format_tokens(row.get("tokens")),
                "tokens_raw": _int_value(row.get("tokens")),
                "cost": format_money(row.get("cost")),
                "messages": f"{_int_value(row.get('messages')):,}",
            }
            for name, row in sorted(apps.items(), key=lambda item: _int_value(item[1].get("tokens")), reverse=True)
        ],
        "models": [
            {
                "name": row.get("name", "unknown"),
                "tokens": format_tokens(row.get("tokens")),
                "tokens_raw": _int_value(row.get("tokens")),
                "input": format_tokens(row.get("tokens_in")),
                "output": format_tokens(row.get("tokens_out")),
                "cache": format_tokens(row.get("tokens_cache")),
                "cost": format_money(row.get("cost")),
            }
            for row in models[:12]
        ],
    }


class TokdashNativeApp:
    def __init__(self, root: Any):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.period = "today"
        self.colors = {
            "bg": "#f6f3ee",
            "panel": "#fffaf2",
            "ink": "#221f1a",
            "muted": "#766f64",
            "line": "#ddd2c2",
            "accent": "#2f6f5e",
            "accent_weak": "#d9ebe3",
            "warn": "#a45f2b",
        }
        self.period_buttons: dict[str, Any] = {}
        self.metric_value_labels: list[Any] = []
        self.metric_delta_labels: list[Any] = []
        self.status_var = tk.StringVar(value="Loading")
        self.error_var = tk.StringVar(value="")
        self.metric_title_labels: list[Any] = []

        self.root.title("Tokdash")
        self.root.geometry("980x680")
        self.root.minsize(760, 560)
        self.root.configure(bg=self.colors["bg"])
        self._configure_styles()
        self._build_layout()
        self.refresh()

    def _configure_styles(self) -> None:
        style = self.ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=self.colors["panel"], fieldbackground=self.colors["panel"], rowheight=28)
        style.configure("Treeview.Heading", background=self.colors["bg"], foreground=self.colors["muted"], relief="flat")
        style.map("Treeview", background=[("selected", self.colors["accent_weak"])], foreground=[("selected", self.colors["ink"])])

    def _frame(self, parent: Any, **kwargs: Any) -> Any:
        return self.tk.Frame(parent, bg=kwargs.pop("bg", self.colors["bg"]), **kwargs)

    def _label(self, parent: Any, text: str = "", **kwargs: Any) -> Any:
        return self.tk.Label(parent, text=text, bg=kwargs.pop("bg", self.colors["bg"]), fg=kwargs.pop("fg", self.colors["ink"]), **kwargs)

    def _build_layout(self) -> None:
        shell = self._frame(self.root, padx=28, pady=24)
        shell.pack(fill="both", expand=True)

        header = self._frame(shell)
        header.pack(fill="x")
        title_stack = self._frame(header)
        title_stack.pack(side="left", fill="x", expand=True)
        self._label(title_stack, "Tokdash", font=("Avenir Next", 30, "bold")).pack(anchor="w")
        self._label(
            title_stack,
            "Local AI coding usage",
            fg=self.colors["muted"],
            font=("Avenir Next", 13),
        ).pack(anchor="w", pady=(2, 0))

        controls = self._frame(header)
        controls.pack(side="right", anchor="n")
        for label, value in PERIODS.items():
            btn = self.tk.Button(
                controls,
                text=label,
                command=lambda period=value: self.set_period(period),
                bd=0,
                padx=14,
                pady=8,
                cursor="pointinghand",
                font=("Avenir Next", 12, "bold"),
            )
            btn.pack(side="left", padx=(0, 6))
            self.period_buttons[value] = btn
        self.tk.Button(
            controls,
            text="Refresh",
            command=self.refresh,
            bg=self.colors["ink"],
            fg=self.colors["panel"],
            activebackground=self.colors["accent"],
            activeforeground=self.colors["panel"],
            bd=0,
            padx=16,
            pady=8,
            cursor="pointinghand",
            font=("Avenir Next", 12, "bold"),
        ).pack(side="left")

        metrics = self._frame(shell)
        metrics.pack(fill="x", pady=(30, 22))
        for index in range(3):
            panel = self._frame(metrics, bg=self.colors["panel"], padx=18, pady=16, highlightbackground=self.colors["line"], highlightthickness=1)
            panel.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 10, 0))
            title = self._label(panel, "", bg=self.colors["panel"], fg=self.colors["muted"], font=("Avenir Next", 12, "bold"))
            title.pack(anchor="w")
            value = self._label(panel, "0", bg=self.colors["panel"], font=("Avenir Next", 28, "bold"))
            value.pack(anchor="w", pady=(8, 4))
            delta = self._label(panel, "", bg=self.colors["panel"], fg=self.colors["muted"], font=("Avenir Next", 11))
            delta.pack(anchor="w")
            self.metric_title_labels.append(title)
            self.metric_value_labels.append(value)
            self.metric_delta_labels.append(delta)

        body = self._frame(shell)
        body.pack(fill="both", expand=True)

        left = self._frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 14))
        right = self._frame(body)
        right.pack(side="right", fill="both", expand=True)

        self._label(left, "Models", font=("Avenir Next", 16, "bold")).pack(anchor="w", pady=(0, 8))
        self.models_tree = self.ttk.Treeview(left, columns=("model", "tokens", "input", "output", "cache", "cost"), show="headings")
        self._setup_tree(
            self.models_tree,
            [("model", "Model"), ("tokens", "Tokens"), ("input", "Input"), ("output", "Output"), ("cache", "Cache"), ("cost", "Cost")],
        )
        self.models_tree.pack(fill="both", expand=True)

        self._label(right, "Apps", font=("Avenir Next", 16, "bold")).pack(anchor="w", pady=(0, 8))
        self.apps_tree = self.ttk.Treeview(right, columns=("app", "tokens", "cost", "messages"), show="headings", height=8)
        self._setup_tree(self.apps_tree, [("app", "App"), ("tokens", "Tokens"), ("cost", "Cost"), ("messages", "Messages")])
        self.apps_tree.pack(fill="both", expand=True)

        footer = self._frame(shell)
        footer.pack(fill="x", pady=(14, 0))
        self._label(footer, textvariable=self.status_var, fg=self.colors["muted"], font=("Avenir Next", 11)).pack(side="left")
        self._label(footer, textvariable=self.error_var, fg=self.colors["warn"], font=("Avenir Next", 11)).pack(side="right")

    def _setup_tree(self, tree: Any, columns: list[tuple[str, str]]) -> None:
        for key, label in columns:
            tree.heading(key, text=label)
            anchor = "w" if key in {"model", "app"} else "e"
            width = 220 if key == "model" else 110
            tree.column(key, width=width, anchor=anchor, stretch=True)

    def _update_period_buttons(self) -> None:
        for period, button in self.period_buttons.items():
            selected = period == self.period
            button.configure(
                bg=self.colors["accent"] if selected else self.colors["accent_weak"],
                fg=self.colors["panel"] if selected else self.colors["ink"],
                activebackground=self.colors["accent"],
                activeforeground=self.colors["panel"],
            )

    def set_period(self, period: str) -> None:
        self.period = period
        self.refresh()

    def refresh(self) -> None:
        self._update_period_buttons()
        self.status_var.set("Refreshing")
        self.error_var.set("")
        self.root.update_idletasks()
        try:
            view_model = compute_native_dashboard_data(self.period)
        except Exception as exc:
            self.error_var.set(str(exc))
            self.status_var.set("Refresh failed")
            return

        for index, metric in enumerate(view_model["metrics"]):
            self.metric_title_labels[index].configure(text=metric["label"])
            self.metric_value_labels[index].configure(text=metric["value"])
            self.metric_delta_labels[index].configure(text=metric["delta"])

        self._replace_tree_rows(
            self.models_tree,
            [
                (row["name"], row["tokens"], row["input"], row["output"], row["cache"], row["cost"])
                for row in view_model["models"]
            ],
        )
        self._replace_tree_rows(
            self.apps_tree,
            [(row["name"], row["tokens"], row["cost"], row["messages"]) for row in view_model["breakdown"]],
        )
        self.status_var.set(f"{view_model['range_label']} · Updated {view_model['refreshed']}")

    def _replace_tree_rows(self, tree: Any, rows: list[tuple[Any, ...]]) -> None:
        tree.delete(*tree.get_children())
        for row in rows:
            if not row:
                continue
            tree.insert("", "end", values=tuple(row))


def main() -> int:
    import tkinter as tk

    root = tk.Tk()
    TokdashNativeApp(root)
    root.mainloop()
    return 0


def json_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit native Tokdash dashboard data as JSON")
    parser.add_argument("period", nargs="?", default="today", choices=["today", "week", "month"])
    args = parser.parse_args(argv)
    print(json.dumps(compute_native_dashboard_data(args.period)))
    return 0


if __name__ == "__main__":
    import sys

    if "--json" in sys.argv:
        sys.argv.remove("--json")
        raise SystemExit(json_main())
    raise SystemExit(main())
