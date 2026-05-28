import { useState, useEffect } from 'react';

interface Props {
  children: React.ReactNode;
}

const PASSWORD = 'catalyst2026';

export default function PasswordGate({ children }: Props) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    const stored = sessionStorage.getItem('catalyst_auth');
    if (stored === 'true') {
      setIsAuthenticated(true);
    }
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (password === PASSWORD) {
      sessionStorage.setItem('catalyst_auth', 'true');
      setIsAuthenticated(true);
      setError('');
    } else {
      setError('Invalid password');
      setPassword('');
    }
  };

  if (isAuthenticated) {
    return <>{children}</>;
  }

  return (
    <div className="password-gate">
      <div className="password-gate-card">
        <h2>Catalyst Attribution Workbench</h2>
        <form onSubmit={handleSubmit}>
          <input
            type="password"
            placeholder="Enter password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
          />
          <button type="submit">Access</button>
        </form>
        {error && <div className="password-gate-error">{error}</div>}
      </div>
    </div>
  );
}
