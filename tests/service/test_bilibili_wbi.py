from service.bilibili_wbi import BilibiliWbiSigner


def test_wbi_signer_extracts_keys_and_does_not_mutate_input() -> None:
    signer = BilibiliWbiSigner.from_urls(
        "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
        "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
    )
    params = {"oid": 7788, "pagination_str": '{"offset":"CAEiAggC"}'}

    signed = signer.sign(params, timestamp=1_700_000_000)

    assert params == {"oid": 7788, "pagination_str": '{"offset":"CAEiAggC"}'}
    assert signed["wts"] == "1700000000"
    assert signed["w_rid"] == (
        "7ea55bd4de9f13b6b256161fcda586a5"  # stable vector for the fixed keys/time
    )


def test_wbi_signer_replaces_existing_signature_and_filters_unsafe_chars() -> None:
    signer = BilibiliWbiSigner("a" * 32, "b" * 32)

    signed = signer.sign(
        {"w_rid": "old", "value": "hello!'()*world"},
        timestamp=1,
    )

    assert signed["value"] == "helloworld"
    assert signed["w_rid"] != "old"
    assert signed["wts"] == "1"
