import assert from "node:assert/strict";
import test from "node:test";
import {applyConnectionDrop} from "./applyConnectionDrop.js";

// rc-tree 5 drop info: isLeaf and children live on the node. props only
// carries the compatibility fields eventKey, pos, and expanded.
function rcTreeDropInfo(node, dragNode, internalDropPosition) {
    const posParts = String(node.pos).split("-");
    const props = {
        eventKey: node.key,
        pos: node.pos,
        expanded: node.expanded,
    };
    Object.defineProperty(node, "props", {
        get() {
            return props;
        },
    });
    return {
        node,
        dragNode,
        dropToGap: internalDropPosition !== 0,
        dropPosition: internalDropPosition + Number(posParts[posParts.length - 1]),
    };
}

function parentOf(tree, key, parent = null) {
    for (const item of tree) {
        if (item.key === key) {
            return parent;
        }
        if (item.children) {
            const found = parentOf(item.children, key, item);
            if (found !== undefined) {
                return found;
            }
        }
    }
    return undefined;
}

test("a connection dropped on a connection is not nested under it", () => {
    const connectionA = {key: "2", isLeaf: true, children: [], appid: 2};
    const connectionB = {key: "3", isLeaf: true, children: [], appid: 3};
    const folder = {key: "1", isLeaf: false, children: [connectionA, connectionB], appid: 1, expanded: true};
    const tree = [folder];

    const dropTarget = {key: "3", isLeaf: true, children: [], pos: "0-0-1", expanded: false};
    const next = applyConnectionDrop(
        tree,
        rcTreeDropInfo(
            dropTarget,
            {key: "2", isLeaf: true, children: [], pos: "0-0-0"},
            0,
        ),
    );

    assert.equal(next, tree);
    assert.equal(parentOf(next, "2").key, "1");
    assert.equal(connectionB.children.includes(connectionA), false);
    assert.equal(dropTarget.props.isLeaf, undefined);
    assert.equal(dropTarget.props.children, undefined);
    assert.equal(dropTarget.props.eventKey, "3");
});

test("an expanded folder bottom gap reads children from the event node", () => {
    const loose = {key: "13", isLeaf: true, children: [], appid: 13};
    const folder = {
        key: "10",
        isLeaf: false,
        expanded: true,
        appid: 10,
        children: [
            {key: "11", isLeaf: true, children: [], appid: 11},
            {key: "12", isLeaf: true, children: [], appid: 12},
        ],
    };
    const tree = [folder, loose];
    const dropTarget = {
        key: "10",
        isLeaf: false,
        expanded: true,
        pos: "0-0",
        children: folder.children,
    };

    const next = applyConnectionDrop(tree, rcTreeDropInfo(dropTarget, loose, 1));

    assert.equal(next[0].children[0].key, "13");
    assert.equal(parentOf(next, "13").key, "10");
    assert.equal(dropTarget.props.children, undefined);
});
