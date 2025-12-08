"""
Vault module for secure credential management.
Supports: pass (password-store), keyring, or environment variables.
"""

import os
import subprocess
import getpass
from typing import Optional, Tuple


class VaultError(Exception):
    """Vault operation error."""
    pass


class Vault:
    """Secure credential storage abstraction."""
    
    def __init__(self, backend: str = "pass", vault_path: str = "migration/windows"):
        """
        Initialize vault.
        
        Args:
            backend: "pass", "keyring", "env", or "prompt"
            vault_path: Base path in the vault for credentials
        """
        self.backend = backend
        self.vault_path = vault_path
        self._cache = {}  # In-memory cache for session
        
        if backend == "pass":
            self._verify_pass_available()
    
    def _verify_pass_available(self):
        """Verify pass is installed and initialized."""
        try:
            result = subprocess.run(
                ["pass", "ls"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                raise VaultError("pass is not initialized. Run: pass init <gpg-id>")
        except FileNotFoundError:
            raise VaultError("pass is not installed. Run: sudo apt install pass")
    
    def get_credential(self, name: str, username: Optional[str] = None) -> Tuple[str, str]:
        """
        Get credentials (username, password).
        
        Args:
            name: Credential name (e.g., "local-admin")
            username: Optional username override
            
        Returns:
            Tuple of (username, password)
        """
        cache_key = f"{name}:{username}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        if self.backend == "pass":
            cred = self._get_from_pass(name, username)
        elif self.backend == "keyring":
            cred = self._get_from_keyring(name, username)
        elif self.backend == "env":
            cred = self._get_from_env(name, username)
        else:  # prompt
            cred = self._get_from_prompt(name, username)
        
        self._cache[cache_key] = cred
        return cred
    
    def _get_from_pass(self, name: str, username: Optional[str] = None) -> Tuple[str, str]:
        """
        Get credential from pass.
        
        Expected format in pass:
        migration/windows/local-admin:
          password
          username: Administrator
          
        Or just password if username provided.
        """
        path = f"{self.vault_path}/{name}"
        
        try:
            result = subprocess.run(
                ["pass", "show", path],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                raise VaultError(f"Credential not found: {path}")
            
            lines = result.stdout.strip().split('\n')
            password = lines[0]
            
            # Look for username in metadata
            stored_username = username
            for line in lines[1:]:
                if line.lower().startswith('username:'):
                    stored_username = line.split(':', 1)[1].strip()
                    break
            
            if not stored_username:
                stored_username = "Administrator"  # Default
            
            return (stored_username, password)
            
        except subprocess.TimeoutExpired:
            raise VaultError("pass command timed out (GPG agent issue?)")
        except Exception as e:
            raise VaultError(f"Failed to get credential: {e}")
    
    def _get_from_keyring(self, name: str, username: Optional[str] = None) -> Tuple[str, str]:
        """Get credential from system keyring."""
        try:
            import keyring
            service = f"{self.vault_path}/{name}"
            user = username or "Administrator"
            password = keyring.get_password(service, user)
            
            if not password:
                raise VaultError(f"No keyring entry for {service}/{user}")
            
            return (user, password)
        except ImportError:
            raise VaultError("keyring module not installed. Run: pip install keyring")
    
    def _get_from_env(self, name: str, username: Optional[str] = None) -> Tuple[str, str]:
        """Get credential from environment variables."""
        env_prefix = name.upper().replace("-", "_")
        
        user = username or os.environ.get(f"MIGRATION_{env_prefix}_USER", "Administrator")
        password = os.environ.get(f"MIGRATION_{env_prefix}_PASS")
        
        if not password:
            raise VaultError(f"Environment variable MIGRATION_{env_prefix}_PASS not set")
        
        return (user, password)
    
    def _get_from_prompt(self, name: str, username: Optional[str] = None) -> Tuple[str, str]:
        """Prompt user for credentials."""
        print(f"\n🔐 Credentials required for: {name}")
        
        if username:
            user = username
            print(f"   Username: {user}")
        else:
            user = input("   Username: ").strip() or "Administrator"
        
        password = getpass.getpass("   Password: ")
        
        return (user, password)
    
    def set_credential(self, name: str, username: str, password: str) -> bool:
        """
        Store credential in vault.
        
        Args:
            name: Credential name
            username: Username
            password: Password
            
        Returns:
            True if successful
        """
        if self.backend == "pass":
            return self._set_in_pass(name, username, password)
        elif self.backend == "keyring":
            return self._set_in_keyring(name, username, password)
        else:
            raise VaultError(f"Backend {self.backend} does not support storing credentials")
    
    def _set_in_pass(self, name: str, username: str, password: str) -> bool:
        """Store credential in pass."""
        path = f"{self.vault_path}/{name}"
        content = f"{password}\nusername: {username}\n"
        
        try:
            # Use pass insert with multiline
            process = subprocess.Popen(
                ["pass", "insert", "-m", path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(input=content, timeout=10)
            
            if process.returncode != 0:
                raise VaultError(f"Failed to store credential: {stderr}")
            
            return True
            
        except subprocess.TimeoutExpired:
            raise VaultError("pass command timed out")
    
    def _set_in_keyring(self, name: str, username: str, password: str) -> bool:
        """Store credential in system keyring."""
        try:
            import keyring
            service = f"{self.vault_path}/{name}"
            keyring.set_password(service, username, password)
            return True
        except ImportError:
            raise VaultError("keyring module not installed")
    
    def list_credentials(self) -> list:
        """List available credentials."""
        if self.backend == "pass":
            try:
                result = subprocess.run(
                    ["pass", "ls", self.vault_path],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    # Parse tree output
                    creds = []
                    for line in result.stdout.split('\n'):
                        # Remove tree characters
                        clean = line.replace('├──', '').replace('└──', '').replace('│', '').strip()
                        if clean and not clean.startswith(self.vault_path):
                            creds.append(clean)
                    return creds
            except:
                pass
        return []
    
    def clear_cache(self):
        """Clear in-memory credential cache."""
        self._cache.clear()


def get_kerberos_auth(domain: str = None) -> bool:
    """
    Check if we have valid Kerberos tickets.
    
    Returns:
        True if valid tickets exist
    """
    try:
        result = subprocess.run(
            ["klist", "-s"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def kinit(principal: str, password: Optional[str] = None, keytab: Optional[str] = None) -> bool:
    """
    Obtain Kerberos ticket.
    
    Args:
        principal: Kerberos principal (user@REALM)
        password: Password (if not using keytab)
        keytab: Path to keytab file
        
    Returns:
        True if successful
    """
    try:
        if keytab:
            cmd = ["kinit", "-k", "-t", keytab, principal]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        else:
            cmd = ["kinit", principal]
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(input=password + "\n", timeout=30)
            result = process
        
        return result.returncode == 0
        
    except Exception as e:
        print(f"kinit failed: {e}")
        return False


def kdestroy() -> bool:
    """Destroy Kerberos tickets."""
    try:
        result = subprocess.run(["kdestroy"], capture_output=True, timeout=5)
        return result.returncode == 0
    except:
        return False
