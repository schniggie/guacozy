// rc-tree 5 copies isLeaf, children, key, expanded, and pos onto the event
// node. The compatibility node.props getter does not include isLeaf or children.
export function applyConnectionDrop(treeData, info) {
    const dropNode = info.node;
    const dropKey = dropNode.key;
    const dragKey = info.dragNode.key;
    const dropPos = String(dropNode.pos).split("-");
    const dropPosition = info.dropPosition - Number(dropPos[dropPos.length - 1]);

    // A connection is a leaf. Dropping one onto another must not nest them.
    if (dropNode.isLeaf && dropPosition === 0) {
        return treeData;
    }

    const loop = (data, key, callback) => {
        data.forEach((item, index, arr) => {
            if (item.key === key) {
                callback(item, index, arr);
                return;
            }
            if (item.children) {
                loop(item.children, key, callback);
            }
        });
    };

    const data = [...treeData];

    let dragObj;
    loop(data, dragKey, (item, index, arr) => {
        arr.splice(index, 1);
        dragObj = item;
    });

    if (!info.dropToGap) {
        loop(data, dropKey, (item) => {
            item.children = item.children || [];
            item.children.push(dragObj);
        });
    } else if (
        (dropNode.children || []).length > 0 &&
        dropNode.expanded &&
        dropPosition === 1
    ) {
        loop(data, dropKey, (item) => {
            item.children = item.children || [];
            item.children.unshift(dragObj);
        });
    } else {
        let ar;
        let i;
        loop(data, dropKey, (item, index, arr) => {
            ar = arr;
            i = index;
        });
        if (dropPosition === -1) {
            ar.splice(i, 0, dragObj);
        } else {
            ar.splice(i + 1, 0, dragObj);
        }
    }

    return data;
}
