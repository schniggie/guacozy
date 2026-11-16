import React from 'react';
import {createRoot} from 'react-dom/client';
import './compat/findDOMNode';
import './index.css';
import 'flexlayout-react/style/dark.css'
import '../../guacozy_server/static/css/styles.css';
import 'react-contexify/dist/ReactContexify.css';
import App from './Components/App/App';
import {AppProvider} from "./Context/AppContext";
import {LayoutProvider} from "./Layout/LayoutContext";

createRoot(document.getElementById('root')).render(
    <LayoutProvider>
        <AppProvider>
            <App/>
        </AppProvider>
    </LayoutProvider>
);