"""Every guard in clone_args.py has a test that fails if the guard is removed (#10)."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from clone_args import build_clone_argv, should_skip_clone, validate_repo_url  # noqa: E402


class ValidateRepoUrlTests(unittest.TestCase):
    def test_https_public_url_accepted(self):
        self.assertEqual(validate_repo_url('https://github.com/org/repo.git'), (True, 'ok'))

    def test_ssh_scheme_rejected(self):
        ok, reason = validate_repo_url('ssh://git@host/org/repo.git')
        self.assertFalse(ok)
        self.assertIn('https', reason)

    def test_scp_style_git_at_rejected(self):
        self.assertFalse(validate_repo_url('git@host:org/repo.git')[0])

    def test_bare_host_colon_path_rejected(self):
        self.assertFalse(validate_repo_url('host:org/repo.git')[0])

    def test_file_url_rejected(self):
        self.assertFalse(validate_repo_url('file:///srv/repo')[0])

    def test_data_url_rejected(self):
        self.assertFalse(validate_repo_url('data:text/plain,--upload-pack=evil')[0])

    def test_plain_http_rejected(self):
        self.assertFalse(validate_repo_url('http://github.com/org/repo.git')[0])

    def test_userinfo_rejected(self):
        ok, reason = validate_repo_url('https://user:pass@github.com/org/repo.git')
        self.assertFalse(ok)
        self.assertIn('userinfo', reason)

    def test_missing_host_rejected(self):
        self.assertFalse(validate_repo_url('https:///no-host')[0])

    def test_whitespace_in_url_rejected(self):
        self.assertFalse(validate_repo_url('https://host/... --upload-pack=x')[0])

    def test_empty_url_rejected(self):
        self.assertFalse(validate_repo_url('')[0])


class BuildArgvTests(unittest.TestCase):
    def test_argv_form_always_has_double_dash_separator(self):
        argv = build_clone_argv('https://github.com/org/repo.git', '/workspace/repo')
        self.assertEqual(argv, ['git', 'clone', '--', 'https://github.com/org/repo.git', '/workspace/repo'])

    def test_refuses_bad_url(self):
        with self.assertRaises(ValueError):
            build_clone_argv('ssh://git@host/org/repo.git', '/workspace/repo')

    def test_refuses_dest_starting_with_dash(self):
        with self.assertRaises(ValueError):
            build_clone_argv('https://github.com/org/repo.git', '--upload-pack=evil')

    def test_refuses_empty_dest(self):
        with self.assertRaises(ValueError):
            build_clone_argv('https://github.com/org/repo.git', '')

    def test_dash_shaped_url_stays_single_argv_element_after_separator(self):
        argv = build_clone_argv('https://host/-o', '/workspace/repo')
        self.assertIn('--', argv)
        self.assertEqual(argv.count('https://host/-o'), 1)
        self.assertGreater(argv.index('https://host/-o'), argv.index('--'))
        self.assertNotIn('-o', [part for part in argv if part != 'https://host/-o'])

    def test_upload_pack_injection_shape_is_rejected_never_split(self):
        url = 'https://host/... --upload-pack=x'
        with self.assertRaises(ValueError):
            build_clone_argv(url, '/workspace/repo')


class ShouldSkipCloneTests(unittest.TestCase):
    def test_non_empty_listing_skips_clone(self):
        self.assertTrue(should_skip_clone('/workspace/repo', ['README.md']))

    def test_hidden_only_listing_still_skips(self):
        self.assertTrue(should_skip_clone('/workspace/repo', ['.git']))

    def test_empty_listing_clones(self):
        self.assertFalse(should_skip_clone('/workspace/repo', []))

    def test_missing_dir_none_listing_clones(self):
        self.assertFalse(should_skip_clone('/workspace/repo', None))


if __name__ == '__main__':
    unittest.main()
