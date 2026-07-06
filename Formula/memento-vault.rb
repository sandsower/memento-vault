class MementoVault < Formula
  desc "Persistent memory layer for AI coding agents"
  homepage "https://github.com/sandsower/memento-vault"
  url "https://github.com/sandsower/memento-vault/archive/refs/tags/v5.0.0.tar.gz"
  # sha256 "UPDATE_WITH_ACTUAL_SHA256_AFTER_RELEASE"
  license "MIT"
  head "https://github.com/sandsower/memento-vault.git", branch: "main"

  depends_on "git"
  depends_on "python@3"

  def install
    # Install the full project tree into libexec
    ignored_hidden_files = %w[. .. .git]
    libexec.install Dir["*"]
    libexec.install Dir[".*"].reject { |f| ignored_hidden_files.include?(File.basename(f)) }

    # Link the CLI wrapper
    bin.install_symlink libexec/"bin/memento-vault"
  end

  def caveats
    <<~EOS
      To complete setup, run:
        memento-vault install

      This will configure agent hooks, skills, and initialize your vault.

      Optional flags:
        memento-vault install --experimental   # Tenet retrieval + Inception consolidation
        memento-vault install --mcp            # MCP server (Cursor, Windsurf, Claude Code, etc.)
        memento-vault install --remote URL     # Connect to a remote vault
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/memento-vault version").strip
    assert_match "Usage: memento-vault warmup", shell_output("#{bin}/memento-vault warmup --help")
  end
end
