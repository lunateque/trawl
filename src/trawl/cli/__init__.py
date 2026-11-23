import typer

from trawl.cli.add import add as _add
from trawl.cli.context import options as _options
from trawl.cli.plan import import_command as _import_command
from trawl.cli.plan import plan as _plan
from trawl.cli.review import explain as _explain
from trawl.cli.review import pick as _pick
from trawl.cli.review import retry as _retry
from trawl.cli.review import skip as _skip
from trawl.cli.review import status as _status
from trawl.cli.run import run as _run
from trawl.cli.run import tui as _tui
from trawl.cli.run import upgrade as _upgrade
from trawl.cli.system import doctor_command as _doctor_command
from trawl.cli.system import service as _service
from trawl.cli.taste import auth as _auth
from trawl.cli.taste import follow as _follow
from trawl.cli.taste import link as _link
from trawl.cli.taste import sync_taste_command as _sync_taste_command
from trawl.cli.taste import unfollow as _unfollow
from trawl.cli.taste import unresolved as _unresolved

app = typer.Typer(help="Plan a tagged local music library with MusicBrainz.", no_args_is_help=True)

app.callback()(_options)
app.command()(_add)
app.command()(_plan)
app.command()(_run)
app.command()(_tui)
app.command("import")(_import_command)
app.command()(_upgrade)
app.command()(_retry)
app.command()(_pick)
app.command()(_skip)
app.command()(_status)
app.command()(_explain)
app.command("doctor")(_doctor_command)
app.command("sync-taste")(_sync_taste_command)
app.command()(_link)
app.command()(_unresolved)
app.command()(_follow)
app.command()(_unfollow)
app.command()(_auth)
app.command()(_service)
