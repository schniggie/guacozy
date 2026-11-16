import * as ReactDOM from 'react-dom';
import 'react-dom/client';

// semantic-ui-react 2.1.5 mounts through @fluentui/react-component-ref,
// which calls ReactDOM.findDOMNode. React 19 keeps that walk on its client
// internals and publishes the public export as null.
const cjsModule = ReactDOM.default ?? ReactDOM;
const internals = ReactDOM.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE
    ?? cjsModule.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
const findDOMNode = internals?.findDOMNode;

if (typeof findDOMNode !== 'function') {
    throw new Error('React 19 findDOMNode compat shim could not find the client implementation');
}

function install(target) {
    if (!target || typeof target.findDOMNode === 'function') {
        return;
    }
    try {
        target.findDOMNode = findDOMNode;
    } catch {
        // A frozen module namespace cannot grow new exports. The shared
        // CommonJS exports object, and Vite's interop object, can.
    }
}

install(cjsModule);
install(ReactDOM);

if (typeof cjsModule.findDOMNode !== 'function' && typeof ReactDOM.findDOMNode !== 'function') {
    throw new Error('React 19 findDOMNode compat shim could not install the client implementation');
}
