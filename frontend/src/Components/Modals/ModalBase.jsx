import React from 'react';
import {Modal} from "../../ui/ui";

function ModalBase({children, handleClose}) {
    return (
        <Modal
            open={true}
            onClose={handleClose}
            basic
            size='small'
            centered={false}
            closeOnDimmerClick={false}
            onKeyDown={(event) => {
                let code = event.charCode || event.keyCode;
                code === 27 && handleClose();
            }}
        >
            {children}
        </Modal>
    );
}

export default ModalBase;