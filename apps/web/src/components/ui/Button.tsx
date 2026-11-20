import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/cn';

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-body-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-45',
  {
    variants: {
      variant: {
        primary: 'border border-transparent bg-accent text-accent-ink hover:bg-accent-hover',
        secondary: 'border border-line-control bg-bg-200 text-ink-100 hover:bg-bg-300',
        ghost: 'border border-transparent bg-transparent text-ink-200 hover:bg-bg-300 hover:text-ink-100',
        danger: 'border border-down-soft bg-down-soft text-down hover:border-down',
      },
      size: { sm: 'control-sm px-2 text-caption', md: 'control-md px-3' },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
);
export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'type'>, VariantProps<typeof buttonVariants> {
  icon?: ReactNode;
}
export function Button({ className, variant, size, icon, children, ...props }: ButtonProps) {
  return (
    <button type="button" className={cn(buttonVariants({ variant, size }), className)} {...props}>
      {icon}
      {children}
    </button>
  );
}
