export const PROVIDER_WIDGET_COUNT = 12;

export function resolvePasswordWidgetWidth({
    drawingOnMainGraphCanvas,
    nodeWidth,
    requestedWidth,
}) {
    const normalizedNodeWidth = Number(nodeWidth);
    const normalizedRequestedWidth = Number(requestedWidth);
    if (
        drawingOnMainGraphCanvas
        && Number.isFinite(normalizedNodeWidth)
        && normalizedNodeWidth > 0
    ) {
        return normalizedNodeWidth;
    }
    if (Number.isFinite(normalizedRequestedWidth) && normalizedRequestedWidth > 0) {
        return normalizedRequestedWidth;
    }
    if (Number.isFinite(normalizedNodeWidth) && normalizedNodeWidth > 0) {
        return normalizedNodeWidth;
    }
    return 30;
}

export function migrateProviderWidgetValues(values) {
    if (!Array.isArray(values)) {
        return values;
    }
    if (values.length === PROVIDER_WIDGET_COUNT) {
        return values;
    }

    // v0.4 and earlier:
    // [api_key, base_url, model, protocol, generate_endpoint, edit_endpoint]
    if (
        values.length === 6
        && typeof values[4] === "string"
        && typeof values[5] === "string"
    ) {
        const generateEndpoint = values[4];
        const editEndpoint = values[5];
        return [
            ...values.slice(0, 4),
            Boolean(generateEndpoint.trim() || editEndpoint.trim()),
            generateEndpoint,
            editEndpoint,
            true,
            "",
            "",
            "",
            "",
        ];
    }

    // v0.5:
    // [api_key, base_url, model, protocol, use_custom, use_async,
    //  generate_endpoint, edit_endpoint]
    if (
        values.length === 8
        && typeof values[4] === "boolean"
        && typeof values[5] === "boolean"
        && typeof values[6] === "string"
        && typeof values[7] === "string"
    ) {
        return [
            ...values.slice(0, 4),
            values[4],
            values[6],
            values[7],
            values[5],
            "",
            "",
            "",
            "",
        ];
    }

    return values;
}

export function migrateLegacyWorkflowData(data) {
    const values = data?.widgets_values;
    const migrated = migrateProviderWidgetValues(values);
    if (migrated === values) {
        return data;
    }
    return {
        ...data,
        widgets_values: migrated,
    };
}
