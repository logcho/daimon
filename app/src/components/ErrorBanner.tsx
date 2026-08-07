interface Props {
  message: string | null;
}

export function ErrorBanner({ message }: Props) {
  if (!message) return null;
  return (
    <div className="border-t border-red-200 bg-red-50 px-3 py-1.5 text-xs text-red-700">
      {message}
    </div>
  );
}
