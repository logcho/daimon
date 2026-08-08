interface Props {
  message: string | null;
}

export function ErrorBanner({ message }: Props) {
  if (!message) return null;
  return (
    <div className="border-t border-red-400/20 bg-red-500/15 px-3 py-1.5 text-xs text-red-300">
      {message}
    </div>
  );
}
