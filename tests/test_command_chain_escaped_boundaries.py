"""EC1 chain splitting must understand JSON-escaped outer quotes on Windows."""
from easychange.core.command_parser import CommandParser


def test_chain_with_escaped_windows_path_semicolon_keeps_one_command():
    parser=CommandParser()
    raw=r':read \"C:\Program Files\A;B.cs\";:state'
    assert parser.split_chain(raw)==[
        (r':read \"C:\Program Files\A;B.cs\"',False),
        (':state',False),
    ]
    assert parser.parse(parser.split_chain(raw)[0][0])==[
        'read',r'C:\Program Files\A;B.cs'
    ]


def test_chain_with_escaped_windows_path_and_and_operator():
    parser=CommandParser()
    raw=r':open \"C:\A&&B.cs\"&&:state'
    assert parser.split_chain(raw)==[
        (r':open \"C:\A&&B.cs\"',True),
        (':state',False),
    ]
