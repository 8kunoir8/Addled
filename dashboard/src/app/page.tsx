'use client';

import { useEffect } from 'react';
import Link from 'next/link';

export default function Home() {
  // This used to be `redirect('/chat')` from next/navigation. That cannot work
  // in a static export: there is no server to answer with a 3xx, so Next emits
  // an error page into out/index.html instead. The desktop app hid it because
  // the backend's nav polling moved the window on to /chat a moment later — but
  // a browser opening the root directly (which is exactly what happens after
  // signing in remotely) saw the error page.
  useEffect(() => {
    window.location.replace('/chat');
  }, []);

  return (
    <div className="flex h-full items-center justify-center text-[#8b949e]">
      <div className="text-center">
        <div className="animate-spin text-2xl mb-3">⟳</div>
        <p>Opening the dashboard…</p>
        <p className="mt-3 text-sm">
          <Link href="/chat" className="text-[#3380FF] hover:underline">
            Go to chat
          </Link>
        </p>
      </div>
    </div>
  );
}
