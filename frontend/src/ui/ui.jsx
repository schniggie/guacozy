import React, {useEffect, useRef} from "react";
import {createPortal} from "react-dom";

const ICONS = {
    "folder outline": "M3 7h6l2 2h10v10H3z",
    computer: "M4 5h16v10H4zM8 19h8M12 15v4",
    terminal: "M4 6h16v12H4zM7 10l3 2-3 2M12 14h5",
    checkmark: "M5 12l5 5L20 7",
    delete: "M6 6l12 12M18 6L6 18",
    dropdown: "M6 9l6 6 6-6",
    "times circle outline": "M12 4a8 8 0 100 16 8 8 0 000-16zM9 9l6 6M15 9l-6 6",
    refresh: "M20 12a8 8 0 11-2.3-5.7M20 4v6h-6",
    bars: "M4 7h16M4 12h16M4 17h16",
    "arrow up": "M12 19V5M6 11l6-6 6 6",
    list: "M8 7h12M8 12h12M8 17h12M4 7h.01M4 12h.01M4 17h.01",
    save: "M5 4h11l3 3v13H5zM8 4v5h7M8 20v-6h8v6",
    "play circle": "M12 4a8 8 0 100 16 8 8 0 000-16zM10 9l6 3-6 3z",
    "stop circle": "M12 4a8 8 0 100 16 8 8 0 000-16zM9 9h6v6H9z",
    time: "M12 7v5l3 2M12 4a8 8 0 100 16 8 8 0 000-16z",
    browser: "M4 5h16v14H4zM4 9h16",
};

export function Icon({name, size, color, className, onClick, link, inverted, ...rest}) {
    const classes = [
        "gc-icon",
        size && `gc-icon-${size}`,
        color && `gc-icon-${color}`,
        onClick && "gc-icon-button",
        className,
    ].filter(Boolean).join(" ");
    const svg = (
        <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d={ICONS[name] || ICONS.computer} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round"/>
        </svg>
    );
    if (onClick) {
        return (
            <button type="button" className={classes} aria-label={name} onClick={onClick} {...rest}>
                {svg}
            </button>
        );
    }
    return <span className={classes} {...rest}>{svg}</span>;
}

export function Button({
    icon,
    children,
    color,
    basic,
    inverted,
    size,
    loading,
    className,
    type = "button",
    disabled,
    ...rest
}) {
    const classes = [
        "gc-button",
        color && `gc-button-${color}`,
        basic && "gc-button-basic",
        inverted && "gc-button-inverted",
        size && `gc-button-${size}`,
        loading && "is-loading",
        className,
    ].filter(Boolean).join(" ");
    return (
        <button type={type} className={classes} disabled={disabled || loading} aria-busy={loading || undefined} {...rest}>
            {icon ? <Icon name={icon}/> : null}
            {children}
        </button>
    );
}

export function Container({className, children}) {
    return <div className={["gc-container", className].filter(Boolean).join(" ")}>{children}</div>;
}

export function Segment({className, children}) {
    return <section className={["gc-segment", className].filter(Boolean).join(" ")}>{children}</section>;
}

export function SegmentGroup({className, children}) {
    return <div className={["gc-segments", className].filter(Boolean).join(" ")}>{children}</div>;
}

export function Divider() {
    return <hr className="gc-divider"/>;
}

export const Input = React.forwardRef(function Input({className, children, icon, fluid, size, ...rest}, ref) {
    void icon;
    void fluid;
    void size;
    return (
        <div className={["gc-input", className].filter(Boolean).join(" ")}>
            <input ref={ref} {...rest}/>
            {children}
        </div>
    );
});

export function Header({icon, content, children}) {
    return (
        <h2 className="gc-header">
            {icon ? <Icon name={icon}/> : null}
            {content || children}
        </h2>
    );
}

export function Message({content, children}) {
    return <p className="gc-message" role="alert">{content || children}</p>;
}

export function Dimmer({active, children}) {
    if (!active) {
        return null;
    }
    return <div className="gc-dimmer">{children}</div>;
}

export function Loader({children}) {
    return <div className="gc-loader" role="status">{children}</div>;
}

export function Modal({children, onKeyDown}) {
    const dialog = useRef(null);
    useEffect(() => {
        dialog.current && dialog.current.focus();
    }, []);
    return createPortal(
        <div className="gc-modal-dim">
            <div
                className="gc-modal"
                role="dialog"
                aria-modal="true"
                tabIndex={-1}
                ref={dialog}
                onKeyDown={onKeyDown}
            >
                {children}
            </div>
        </div>,
        document.body,
    );
}

function ModalContent({children}) {
    return <div className="gc-modal-content">{children}</div>;
}

function ModalActions({children}) {
    return <div className="gc-modal-actions">{children}</div>;
}

Modal.Content = ModalContent;
Modal.Actions = ModalActions;

export function Confirm({header, content, open, onCancel, onConfirm, confirmButton, cancelButton}) {
    if (!open) {
        return null;
    }
    return (
        <Modal onKeyDown={(event) => event.key === "Escape" && onCancel()}>
            <Header content={header}/>
            <ModalContent><p>{content}</p></ModalContent>
            <ModalActions>
                <Button onClick={onCancel}>{cancelButton}</Button>
                <Button color="green" onClick={onConfirm}>{confirmButton}</Button>
            </ModalActions>
        </Modal>
    );
}

export function Accordion({className, children}) {
    return <div className={["gc-accordion", className].filter(Boolean).join(" ")}>{children}</div>;
}

function AccordionTitle({active, onClick, children}) {
    return (
        <button type="button" className="gc-accordion-title" aria-expanded={active ? "true" : "false"} onClick={onClick}>
            {children}
        </button>
    );
}

function AccordionContent({active, children}) {
    if (!active) {
        return null;
    }
    return <div className="gc-accordion-content">{children}</div>;
}

Accordion.Title = AccordionTitle;
Accordion.Content = AccordionContent;

export function List({className, children}) {
    return <ul className={["gc-list", className].filter(Boolean).join(" ")}>{children}</ul>;
}

function ListItem({icon, content, children}) {
    return (
        <li>
            {icon ? <Icon name={icon}/> : null}
            {content || children}
        </li>
    );
}

List.Item = ListItem;

export function Form({className, children, onSubmit}) {
    return (
        <form className={["gc-form", className].filter(Boolean).join(" ")} onSubmit={onSubmit}>
            {children}
        </form>
    );
}

function FormField({children}) {
    return <div className="gc-field">{children}</div>;
}

function optionValue(raw) {
    if (raw === "") {
        return null;
    }
    const numeric = Number(raw);
    return Number.isNaN(numeric) ? raw : numeric;
}

function FormDropdown({name, label, options = [], value, onChange, placeholder}) {
    return (
        <label className="gc-field">
            {label ? <span>{label}</span> : null}
            <select
                className="gc-select"
                name={name}
                value={value ?? ""}
                onChange={(event) => onChange(event, {
                    name,
                    value: optionValue(event.target.value),
                    checked: false,
                })}
            >
                {placeholder ? <option value="">{placeholder}</option> : null}
                {options.map((option) => (
                    <option key={option.key || option.value} value={option.value}>{option.text}</option>
                ))}
            </select>
        </label>
    );
}

function FormGroup({children}) {
    return <div className="gc-form-group">{children}</div>;
}

function FormCheckbox({name, label, onChange, checked}) {
    return (
        <label className="gc-check">
            <input
                type="checkbox"
                name={name}
                checked={!!checked}
                onChange={(event) => onChange(event, {name, value: undefined, checked: event.target.checked})}
            />
            <span>{label}</span>
        </label>
    );
}

Form.Field = FormField;
Form.Dropdown = FormDropdown;
Form.Group = FormGroup;
Form.Checkbox = FormCheckbox;
