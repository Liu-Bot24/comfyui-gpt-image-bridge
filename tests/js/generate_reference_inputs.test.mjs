import assert from "node:assert/strict";
import test from "node:test";

import {
    MAX_GENERATE_REFERENCE_INPUTS,
    MIN_GENERATE_REFERENCE_INPUTS,
    desiredGenerateReferenceInputCount,
    migrateLegacyGenerateReferences,
    normalizeGenerateReferenceInputs,
} from "../../gpt_image_bridge/web/generate_reference_inputs.js";

function input(name, connected = false) {
    return { name, type: "IMAGE", link: connected ? 1 : null };
}

function fakeNode(referenceInputs) {
    return {
        inputs: [
            { name: "provider", type: "GPT_IMAGE_PROVIDER", link: 10 },
            ...referenceInputs,
        ],
        addInput(name, type) {
            this.inputs.push({ name, type, link: null });
        },
        removeInput(index) {
            this.inputs.splice(index, 1);
        },
    };
}

test("defaults to three reference inputs", () => {
    const node = fakeNode(
        Array.from({ length: MAX_GENERATE_REFERENCE_INPUTS }, (_, index) => (
            input(`reference_${index + 1}`)
        )),
    );
    normalizeGenerateReferenceInputs(node);
    assert.deepEqual(
        node.inputs.slice(1).map(({ name }) => name),
        ["reference_1", "reference_2", "reference_3"],
    );
});

test("recomputes height after changing inputs without changing user width", () => {
    const node = fakeNode(
        Array.from({ length: MAX_GENERATE_REFERENCE_INPUTS }, (_, index) => (
            input(`reference_${index + 1}`)
        )),
    );
    node.size = [420, 900];
    node.computeSize = (currentSize) => {
        assert.deepEqual(currentSize, [420, 900]);
        return [280, 360];
    };

    normalizeGenerateReferenceInputs(node);

    assert.deepEqual(node.size, [420, 360]);
});

test("adds one trailing input as the current last input is connected", () => {
    const node = fakeNode([
        input("reference_1", true),
        input("reference_2", true),
        input("reference_3", true),
    ]);
    normalizeGenerateReferenceInputs(node);
    assert.deepEqual(
        node.inputs.slice(1).map(({ name, link }) => [name, link]),
        [
            ["reference_1", 1],
            ["reference_2", 1],
            ["reference_3", 1],
            ["reference_4", null],
        ],
    );
});

test("compacts a disconnected gap without changing connected order", () => {
    const node = fakeNode([
        input("reference_1", true),
        input("reference_2", false),
        input("reference_3", true),
        input("reference_4", false),
    ]);
    normalizeGenerateReferenceInputs(node);
    assert.deepEqual(
        node.inputs.slice(1).map(({ name, link }) => [name, link]),
        [
            ["reference_1", 1],
            ["reference_2", 1],
            ["reference_3", null],
        ],
    );
});

test("never grows past nine inputs", () => {
    const node = fakeNode(
        Array.from({ length: MAX_GENERATE_REFERENCE_INPUTS }, (_, index) => (
            input(`reference_${index + 1}`, true)
        )),
    );
    normalizeGenerateReferenceInputs(node);
    assert.equal(node.inputs.length - 1, MAX_GENERATE_REFERENCE_INPUTS);
    assert.equal(
        desiredGenerateReferenceInputCount(node.inputs.slice(1)),
        MAX_GENERATE_REFERENCE_INPUTS,
    );
    assert.equal(MIN_GENERATE_REFERENCE_INPUTS, 3);
});

test("migrates an old Reference List connection into direct image links", () => {
    let nextLink = 300;
    const sourceOne = {
        id: 1,
        connect(originSlot, target, targetSlot) {
            target.inputs[targetSlot].link = nextLink++;
        },
    };
    const sourceTwo = {
        id: 2,
        connect(originSlot, target, targetSlot) {
            target.inputs[targetSlot].link = nextLink++;
        },
    };
    const listNode = {
        id: 10,
        type: "GPTImageBridgeReferenceList",
        inputs: [
            { name: "image_2", link: 101 },
            { name: "image_3", link: 102 },
        ],
    };
    const graph = {
        links: {
            101: { origin_id: 1, origin_slot: 0 },
            102: { origin_id: 2, origin_slot: 0 },
            200: { origin_id: 10, origin_slot: 0 },
        },
        getNodeById(id) {
            return {
                1: sourceOne,
                2: sourceTwo,
                10: listNode,
            }[id];
        },
    };
    const node = fakeNode([]);
    node.graph = graph;
    node.inputs.push({
        name: "references",
        type: "GPT_IMAGE_REFERENCES",
        link: 200,
    });

    const migration = migrateLegacyGenerateReferences(node);
    assert.deepEqual(migration, { removed: 1, migrated: 2, unresolved: 0 });
    assert.deepEqual(
        node.inputs.slice(1).map(({ name, link }) => [name, link !== null]),
        [
            ["reference_1", true],
            ["reference_2", true],
            ["reference_3", false],
        ],
    );
    assert.equal(
        node.inputs.some(({ name }) => name === "references"),
        false,
    );
});

test("removes an unknown legacy list and reports that it needs attention", () => {
    const node = fakeNode([]);
    node.graph = {
        links: {
            200: { origin_id: 99, origin_slot: 0 },
        },
        getNodeById() {
            return { id: 99, type: "UnknownReferenceSource", inputs: [] };
        },
    };
    node.inputs.push({
        name: "references（Reference 1 开始）",
        type: "GPT_IMAGE_REFERENCES",
        link: 200,
    });
    const migration = normalizeGenerateReferenceInputs(node);
    assert.deepEqual(migration, { removed: 1, migrated: 0, unresolved: 1 });
    assert.deepEqual(
        node.inputs.slice(1).map(({ name }) => name),
        ["reference_1", "reference_2", "reference_3"],
    );
});
