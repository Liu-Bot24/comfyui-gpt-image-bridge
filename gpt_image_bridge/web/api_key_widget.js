import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
    migrateLegacyWorkflowData,
    resolvePasswordWidgetWidth,
} from "./provider_widget_migration.js";

const NODE_TYPE = "GPTImageBridgeAPIProvider";
const ROUTE = "/gpt-image-bridge/session-credential";
const PASSWORD_WIDGET_TYPE = "gpt_image_bridge_password";
const PASSWORD_MASK = "••••••••••••";
const WIDGET_LABELS = {
    use_custom_endpoints: "使用自定义端点",
    generate_endpoint: "自定义生成端点",
    edit_endpoint: "自定义编辑端点",
    use_async: "使用异步",
    async_generate_endpoint: "自定义异步生成提交端点",
    async_edit_endpoint: "自定义异步编辑提交端点",
    async_poll_endpoint_template: "自定义异步查询端点模板",
    async_mapping_json: "自定义异步 JSON 映射",
};

function widgetByName(node, name) {
    return node.widgets?.find((widget) => widget.name === name);
}

function markNodeDirty(node) {
    node.setDirtyCanvas?.(true, true);
    node.graph?.setDirtyCanvas?.(true, true);
}

function applyProviderWidgetLabels(node) {
    for (const [name, label] of Object.entries(WIDGET_LABELS)) {
        const widget = widgetByName(node, name);
        if (widget) {
            widget.label = label;
        }
    }
    markNodeDirty(node);
}

function applyProviderPresentation(node) {
    if (
        node?.type !== NODE_TYPE
        && node?.comfyClass !== NODE_TYPE
    ) {
        return;
    }
    secureApiKeyWidget(node);
    applyProviderWidgetLabels(node);
}

function openPasswordEditor(node, widget) {
    if (widget.__gptImageBridgeEditorOpen) {
        return;
    }
    widget.__gptImageBridgeEditorOpen = true;

    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label", "GPT Image Bridge API Key");
    Object.assign(dialog.style, {
        border: "1px solid #555",
        borderRadius: "10px",
        padding: "18px",
        color: "#eee",
        background: "#262626",
        minWidth: "420px",
        maxWidth: "calc(100vw - 48px)",
        boxShadow: "0 16px 48px rgba(0, 0, 0, 0.5)",
    });

    const form = document.createElement("form");
    form.method = "dialog";
    Object.assign(form.style, {
        display: "grid",
        gap: "12px",
    });

    const label = document.createElement("label");
    label.textContent = "API Key";
    label.htmlFor = "gpt-image-bridge-api-key";
    label.style.fontWeight = "600";

    const input = document.createElement("input");
    input.id = "gpt-image-bridge-api-key";
    input.type = "password";
    input.value = String(widget.value ?? "");
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "Enter API Key";
    Object.assign(input.style, {
        boxSizing: "border-box",
        width: "100%",
        padding: "9px 11px",
        border: "1px solid #666",
        borderRadius: "6px",
        color: "#fff",
        background: "#181818",
        font: "inherit",
    });

    const hint = document.createElement("div");
    hint.textContent =
        "Saved in the local workflow. Remove the Key before sharing it.";
    Object.assign(hint.style, {
        color: "#aaa",
        fontSize: "12px",
    });

    const actions = document.createElement("div");
    Object.assign(actions.style, {
        display: "flex",
        justifyContent: "flex-end",
        gap: "8px",
        marginTop: "4px",
    });

    const cancel = document.createElement("button");
    cancel.type = "submit";
    cancel.value = "cancel";
    cancel.textContent = "Cancel";

    const save = document.createElement("button");
    save.type = "submit";
    save.value = "save";
    save.textContent = "Save";

    actions.append(cancel, save);
    form.append(label, input, hint, actions);
    dialog.append(form);
    document.body.append(dialog);

    dialog.addEventListener(
        "close",
        () => {
            if (dialog.returnValue === "save") {
                const oldValue = widget.value;
                widget.value = input.value.trim();
                widget.callback?.(widget.value, app.canvas, node);
                node.onWidgetChanged?.(
                    widget.name,
                    widget.value,
                    oldValue,
                    widget,
                );
            }
            widget.__gptImageBridgeEditorOpen = false;
            dialog.remove();
            markNodeDirty(node);
        },
        { once: true },
    );

    dialog.showModal();
    input.focus();
    input.select();
}

function turnIntoPasswordWidget(node, widget) {
    widget.type = PASSWORD_WIDGET_TYPE;
    widget.draw = function (ctx, _node, width, y, height) {
        // The Vue legacy sidebar writes its own canvas width into widget.width
        // and may leave that value behind. The classic graph renderer then
        // passes the stale sidebar width here. Clamp only on the main graph
        // canvas; a sidebar-owned canvas must retain its own requested width.
        const drawWidth = resolvePasswordWidgetWidth({
            drawingOnMainGraphCanvas: ctx?.canvas === app.canvas?.canvas,
            nodeWidth: _node?.size?.[0] ?? node?.size?.[0],
            requestedWidth: width,
        });
        const valuePresent = String(this.value ?? "").length > 0;
        const displayValue = valuePresent ? PASSWORD_MASK : "not set";

        ctx.save();
        ctx.beginPath();
        ctx.roundRect(15, y, Math.max(1, drawWidth - 30), height, [height * 0.5]);
        ctx.fillStyle = "#222";
        ctx.strokeStyle = "#666";
        ctx.fill();
        ctx.stroke();

        ctx.font = "12px sans-serif";
        ctx.textBaseline = "middle";
        ctx.textAlign = "left";
        ctx.fillStyle = "#aaa";
        ctx.fillText("api_key", 26, y + height / 2);

        ctx.textAlign = "right";
        ctx.fillStyle = valuePresent ? "#ddd" : "#888";
        ctx.fillText(displayValue, drawWidth - 26, y + height / 2);
        ctx.restore();
    };
    widget.onClick = function () {
        openPasswordEditor(node, widget);
    };
    widget.mouse = function (event) {
        const eventType = String(event?.type ?? "");
        if (eventType === "pointerdown" || eventType === "mousedown") {
            openPasswordEditor(node, widget);
            return true;
        }
        return false;
    };
}

function secureApiKeyWidget(node) {
    const widget = widgetByName(node, "api_key");
    if (!widget) {
        return;
    }
    if (widget.__gptImageBridgeSecured) {
        return;
    }

    // ComfyUI keeps the widget value in local workflow JSON. Queue
    // serialization is separate: serializeValue sends only a one-time handle.
    widget.options ??= {};
    widget.options.serialize = true;
    widget.serializeValue = async () => {
        const apiKey = String(widget.value ?? "").trim();
        if (!apiKey) {
            return "";
        }
        const response = await api.fetchApi(ROUTE, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: apiKey }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.credential_handle) {
            throw new Error("GPT Image Bridge could not create a temporary API credential.");
        }
        return payload.credential_handle;
    };
    turnIntoPasswordWidget(node, widget);
    widget.__gptImageBridgeSecured = true;
    markNodeDirty(node);
}

app.registerExtension({
    name: "GPTImageBridge.ApiKeyProtection",
    nodeCreated(node) {
        applyProviderPresentation(node);
    },
    loadedGraphNode(node) {
        applyProviderPresentation(node);
    },
    afterConfigureGraph() {
        for (const node of app.graph?._nodes ?? []) {
            applyProviderPresentation(node);
        }
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) {
            return;
        }

        const originalOnNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = originalOnNodeCreated?.apply(this, arguments);
            secureApiKeyWidget(this);
            applyProviderWidgetLabels(this);
            return result;
        };

        const originalConfigure = nodeType.prototype.configure;
        nodeType.prototype.configure = function (data) {
            const migrated = migrateLegacyWorkflowData(data);
            const result = originalConfigure?.call(this, migrated);
            // Current ComfyUI may restore widget metadata after onConfigure.
            // Apply the presentation layer once more after configuration has
            // finished so loaded workflows retain the public Chinese labels.
            secureApiKeyWidget(this);
            applyProviderWidgetLabels(this);
            return result;
        };

        const originalOnConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = originalOnConfigure?.apply(this, arguments);
            secureApiKeyWidget(this);
            applyProviderWidgetLabels(this);
            return result;
        };

    },
});
