import { app } from "../../scripts/app.js";
import {
    isGenerateReferenceInput,
    scheduleGenerateReferenceNormalization,
} from "./generate_reference_inputs.js";

const NODE_TYPE = "GPTImageBridgeGenerate";

app.registerExtension({
    name: "GPTImageBridge.GenerateReferenceInputs",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) {
            return;
        }

        const originalOnNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = originalOnNodeCreated?.apply(this, arguments);
            scheduleGenerateReferenceNormalization(this);
            return result;
        };

        const originalOnConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = originalOnConfigure?.apply(this, arguments);
            scheduleGenerateReferenceNormalization(this);
            return result;
        };

        const originalOnConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (
            type,
            index,
            connected,
            linkInfo,
        ) {
            const result = originalOnConnectionsChange?.apply(this, arguments);
            if (
                type !== 2
                && (
                    isGenerateReferenceInput(this.inputs?.[index])
                    || isGenerateReferenceInput(
                        this.inputs?.[linkInfo?.target_slot],
                    )
                )
            ) {
                scheduleGenerateReferenceNormalization(this);
            }
            return result;
        };

        const originalOnDrawForeground = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function () {
            const result = originalOnDrawForeground?.apply(this, arguments);
            if (this.__gptImageBridgeReferenceMigrationWarning) {
                this.__gptImageBridgeReferenceMigrationWarning = false;
                app.extensionManager?.toast?.add({
                    severity: "warn",
                    summary: "GPT Image Bridge",
                    detail: (
                        "旧 Generate 参考列表无法自动拆分，列表连接已移除。"
                        + "请把图片直接连接到 reference_1–reference_9。"
                    ),
                    life: 8000,
                });
            }
            return result;
        };
    },
});
