import React from 'react';
import { HelpCircle } from 'lucide-react';

interface LabelProps {
    children: React.ReactNode;
    htmlFor?: string;
    tooltip?: string;
    className?: string;
}

export const FormLabel = ({ children, htmlFor, tooltip, className = '' }: LabelProps) => (
    <label htmlFor={htmlFor} className={`block text-sm font-medium mb-1.5 flex items-center gap-1.5 ${className}`}>
        {children}
        {tooltip && (
            <div className="group relative">
                <HelpCircle className="w-3.5 h-3.5 text-muted-foreground cursor-help" />
                <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2 py-1 bg-popover text-popover-foreground text-xs rounded shadow-md border border-border whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50">
                    {tooltip}
                </div>
            </div>
        )}
    </label>
);

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
    label?: string;
    tooltip?: string;
    error?: string;
}

export const FormInput = React.forwardRef<HTMLInputElement, InputProps>(
    ({ label, tooltip, error, className = '', ...props }, ref) => (
        <div className="mb-4">
            {label && <FormLabel htmlFor={props.id} tooltip={tooltip}>{label}</FormLabel>}
            <input
                ref={ref}
                className={`flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 ${error ? 'border-destructive' : ''} ${className}`}
                {...props}
            />
            {error && <p className="text-xs text-destructive mt-1">{error}</p>}
        </div>
    )
);

interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
    label?: string;
    tooltip?: string;
    options: { value: string; label: string }[];
    error?: string;
}

export const FormSelect = React.forwardRef<HTMLSelectElement, SelectProps>(
    ({ label, tooltip, options, error, className = '', ...props }, ref) => (
        <div className="mb-4">
            {label && <FormLabel htmlFor={props.id} tooltip={tooltip}>{label}</FormLabel>}
            <div className="relative">
                <select
                    ref={ref}
                    className={`flex h-9 w-full items-center justify-between rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm ring-offset-background placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50 appearance-none ${error ? 'border-destructive' : ''} ${className}`}
                    {...props}
                >
                    {options.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                            {opt.label}
                        </option>
                    ))}
                </select>
                <div className="absolute right-3 top-2.5 pointer-events-none opacity-50">
                    <svg width="10" height="6" viewBox="0 0 10 6" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M1 1L5 5L9 1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                </div>
            </div>
            {error && <p className="text-xs text-destructive mt-1">{error}</p>}
        </div>
    )
);

interface SwitchProps extends React.InputHTMLAttributes<HTMLInputElement> {
    label?: string;
    description?: string;
    tooltip?: string;
}

export const FormSwitch = React.forwardRef<HTMLInputElement, SwitchProps>(
    ({ label, description, tooltip, className = '', ...props }, ref) => (
        <div className="flex items-center justify-between mb-4 p-3 border border-border rounded-lg bg-card/50">
            <div className="space-y-0.5">
                {label && (
                    <div className="flex items-center gap-1.5">
                        <label htmlFor={props.id} className="text-sm font-medium cursor-pointer">
                            {label}
                        </label>
                        {tooltip && (
                            <div className="group relative">
                                <HelpCircle className="w-3.5 h-3.5 text-muted-foreground cursor-help" />
                                <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2 py-1 bg-popover text-popover-foreground text-xs rounded shadow-md border border-border whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50">
                                    {tooltip}
                                </div>
                            </div>
                        )}
                    </div>
                )}
                {description && <p className="text-xs text-muted-foreground">{description}</p>}
            </div>
            <label className="relative inline-flex items-center cursor-pointer">
                <input
                    type="checkbox"
                    ref={ref}
                    className="sr-only peer"
                    {...props}
                />
                <div className="w-9 h-5 bg-input peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-ring rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-background after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary"></div>
            </label>
        </div>
    )
);
