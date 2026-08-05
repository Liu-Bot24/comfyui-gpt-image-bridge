export const GENERATE_REFERENCE_PREFIX = "reference_";
export const MIN_GENERATE_REFERENCE_INPUTS = 3;
export const MAX_GENERATE_REFERENCE_INPUTS = 9;
export const LEGACY_GENERATE_REFERENCE_INPUT_NAMES = new Set([
    "references",
    "references（Reference 1 开始）",
]);

const NORMALIZING_FLAG = "__gptImageBridgeNormalizingReferences";
const SCHEDULED_FLAG = "__gptImageBridgeReferenceNormalizationScheduled";
const LEGACY_REFERENCE_NODE_TYPES = new Set([
    "GPTImageBridgeReferenceList",
    "GPTImageBridgeGenerateReferenceList",
]);

export function referenceNumber(input) {
    const match = /^reference_([1-9])$/.exec(String(input?.name ?? ""));
    return match ? Number(match[1]) : null;
}

export function isGenerateReferenceInput(input) {
    return referenceNumber(input) !== null;
}

function referenceEntries(node) {
    return (node.inputs ?? [])
        .map((input, index) => ({ input, index }))
        .filter(({ input }) => isGenerateReferenceInput(input));
}

function isLegacyGenerateReferenceInput(input) {
    return LEGACY_GENERATE_REFERENCE_INPUT_NAMES.has(String(input?.name ?? ""));
}

function isConnected(input) {
    return input?.link !== null && input?.link !== undefined;
}

function graphLink(graph, linkId) {
    if (linkId === null || linkId === undefined) {
        return null;
    }
    if (graph?.links instanceof Map) {
        return graph.links.get(linkId) ?? null;
    }
    return graph?.links?.[linkId] ?? graph?._links?.get?.(linkId) ?? null;
}

function numberedSourceInput(input) {
    const match = /^(?:image|reference)_([1-9])$/.exec(
        String(input?.name ?? ""),
    );
    return match ? Number(match[1]) : null;
}

function legacyReferenceSources(node, legacyInput) {
    const legacyLink = graphLink(node.graph, legacyInput.link);
    const listNode = node.graph?.getNodeById?.(legacyLink?.origin_id);
    const listType = listNode?.comfyClass ?? listNode?.type;
    if (!listNode || !LEGACY_REFERENCE_NODE_TYPES.has(listType)) {
        return null;
    }
    return (listNode.inputs ?? [])
        .map((input, index) => ({
            input,
            index,
            number: numberedSourceInput(input),
        }))
        .filter(({ input, number }) => number !== null && isConnected(input))
        .sort((left, right) => left.number - right.number)
        .map(({ input }) => {
            const link = graphLink(node.graph, input.link);
            const sourceNode = node.graph?.getNodeById?.(link?.origin_id);
            if (!link || !sourceNode) {
                return null;
            }
            return {
                sourceNode,
                originSlot: link.origin_slot,
            };
        })
        .filter(Boolean);
}

function ensureReferenceInputCount(node, requestedCount) {
    const target = Math.min(
        MAX_GENERATE_REFERENCE_INPUTS,
        Math.max(MIN_GENERATE_REFERENCE_INPUTS, requestedCount),
    );
    let entries = referenceEntries(node);
    while (entries.length < target) {
        node.addInput(
            `${GENERATE_REFERENCE_PREFIX}${entries.length + 1}`,
            "IMAGE",
        );
        entries = referenceEntries(node);
    }
}

export function migrateLegacyGenerateReferences(node) {
    const legacyEntries = (node.inputs ?? [])
        .map((input, index) => ({ input, index }))
        .filter(({ input }) => isLegacyGenerateReferenceInput(input));
    if (!legacyEntries.length) {
        return { removed: 0, migrated: 0, unresolved: 0 };
    }

    const plans = [];
    let unresolved = 0;
    for (const { input } of legacyEntries) {
        if (!isConnected(input)) {
            continue;
        }
        const sources = legacyReferenceSources(node, input);
        if (sources === null) {
            unresolved += 1;
        } else {
            plans.push(...sources);
        }
    }
    for (const { index } of [...legacyEntries].sort((a, b) => b.index - a.index)) {
        node.removeInput(index);
    }

    const alreadyConnected = referenceEntries(node)
        .filter(({ input }) => isConnected(input))
        .length;
    ensureReferenceInputCount(node, alreadyConnected + plans.length);

    let migrated = 0;
    for (const plan of plans) {
        const target = referenceEntries(node)
            .find(({ input }) => !isConnected(input));
        if (!target) {
            unresolved += 1;
            continue;
        }
        plan.sourceNode.connect(plan.originSlot, node, target.index);
        migrated += 1;
    }
    return {
        removed: legacyEntries.length,
        migrated,
        unresolved,
    };
}

export function desiredGenerateReferenceInputCount(referenceInputs) {
    const connectedCount = referenceInputs.filter(isConnected).length;
    if (connectedCount < MIN_GENERATE_REFERENCE_INPUTS) {
        return MIN_GENERATE_REFERENCE_INPUTS;
    }
    return Math.min(
        MAX_GENERATE_REFERENCE_INPUTS,
        connectedCount + (connectedCount < MAX_GENERATE_REFERENCE_INPUTS ? 1 : 0),
    );
}

export function normalizeGenerateReferenceInputs(node) {
    if (!node || node[NORMALIZING_FLAG]) {
        return { removed: 0, migrated: 0, unresolved: 0 };
    }
    node[NORMALIZING_FLAG] = true;
    try {
        const migration = migrateLegacyGenerateReferences(node);
        while (true) {
            const entries = referenceEntries(node);
            const gap = entries.findIndex(
                ({ input }, index) => (
                    !isConnected(input)
                    && entries.slice(index + 1).some(({ input: later }) => isConnected(later))
                ),
            );
            if (gap < 0) {
                break;
            }
            node.removeInput(entries[gap].index);
        }

        let entries = referenceEntries(node);
        const desiredCount = desiredGenerateReferenceInputCount(
            entries.map(({ input }) => input),
        );
        while (entries.length > desiredCount) {
            node.removeInput(entries.at(-1).index);
            entries = referenceEntries(node);
        }
        while (entries.length < desiredCount) {
            node.addInput(
                `${GENERATE_REFERENCE_PREFIX}${entries.length + 1}`,
                "IMAGE",
            );
            entries = referenceEntries(node);
        }

        entries.forEach(({ input }, index) => {
            const name = `${GENERATE_REFERENCE_PREFIX}${index + 1}`;
            input.name = name;
            input.label = name;
        });
        if (Array.isArray(node.size) && typeof node.computeSize === "function") {
            const currentWidth = node.size[0];
            const computedSize = node.computeSize([...node.size]);
            if (
                Array.isArray(computedSize)
                && Number.isFinite(computedSize[1])
            ) {
                node.size[0] = currentWidth;
                node.size[1] = computedSize[1];
            }
        }
        node.setDirtyCanvas?.(true, true);
        node.graph?.setDirtyCanvas?.(true, true);
        return migration;
    } finally {
        node[NORMALIZING_FLAG] = false;
    }
}

export function scheduleGenerateReferenceNormalization(node) {
    if (!node || node[SCHEDULED_FLAG]) {
        return;
    }
    node[SCHEDULED_FLAG] = true;
    setTimeout(() => {
        node[SCHEDULED_FLAG] = false;
        const migration = normalizeGenerateReferenceInputs(node);
        if (migration.unresolved) {
            node.__gptImageBridgeReferenceMigrationWarning = true;
        }
    }, 0);
}
