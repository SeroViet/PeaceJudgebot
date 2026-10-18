import numpy as np
import pytest

from fussball.models import dixon_coles as dc
from fussball.models.devig import fair_probs, overround
from fussball.models.elo import Elo, OrderedLogit
from fussball.models.markets import all_markets, correct_scores, one_x_two, over_under, rps
from fussball.models.pooling import pool


def _simulate(n_rounds=8, seed=1):
    rng = np.random.default_rng(seed)
    teams = [f"T{i}" for i in range(10)]
    att = dict(zip(teams, np.linspace(-0.4, 0.4, 10)))
    dfn = dict(zip(teams, np.linspace(0.3, -0.3, 10)))
    home, away, hg, ag = [], [], [], []
    for _ in range(n_rounds):
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                home.append(h)
                away.append(a)
                hg.append(rng.poisson(np.exp(0.1 + 0.25 + att[h] - dfn[a])))
                ag.append(rng.poisson(np.exp(0.1 + att[a] - dfn[h])))
    return np.array(home), np.array(away), np.array(hg), np.array(ag), att


def test_dixon_coles_recovers_strengths():
    home, away, hg, ag, att = _simulate()
    params = dc.fit(home, away, hg, ag, np.zeros(len(hg)), ridge=0.01)
    idx = params.index()
    est = np.array([params.attack[idx[t]] for t in att])
    assert np.corrcoef(est, list(att.values()))[0, 1] > 0.95
    assert params.home_adv == pytest.approx(0.25, abs=0.08)
    lam, mu = params.rates("T9", "T0")
    assert lam > mu  # stärkstes Team zu Hause gegen schwächstes


def test_ridge_shrinks_toward_prior():
    home, away, hg, ag, _ = _simulate(n_rounds=1)
    strong = dc.fit(home, away, hg, ag, np.zeros(len(hg)), ridge=50.0, prior={"T0": (-0.3, -0.3)})
    idx = strong.index()
    assert strong.attack[idx["T0"]] == pytest.approx(-0.3, abs=0.1)


def test_league_offset_is_estimated():
    home, away, hg, ag, _ = _simulate()
    league = np.zeros(len(hg), dtype=int)
    league[: len(hg) // 2] = 1
    params = dc.fit(home, away, hg, ag, np.zeros(len(hg)), league=league)
    assert np.isfinite(params.mu)


def test_score_matrix_and_markets_are_consistent():
    m = dc.score_matrix(1.6, 1.1, rho=-0.05)
    assert m.sum() == pytest.approx(1.0)
    p = one_x_two(m)
    assert sum(p.values()) == pytest.approx(1.0)
    assert p["H"] > p["A"]
    ou = over_under(m, 2.5)
    assert ou["O"] + ou["U"] == pytest.approx(1.0)
    markets = all_markets(m)
    assert markets["DC"]["1X"] == pytest.approx(p["H"] + p["D"])
    assert markets["OU0.5"]["O"] > markets["OU4.5"]["O"]
    assert correct_scores(m, 1)[0][0] in {"1:1", "1:0"}


def test_rps_perfect_and_worst():
    assert rps({"H": 1, "D": 0, "A": 0}, "H") == 0
    assert rps({"H": 0, "D": 0, "A": 1}, "H") == pytest.approx(1.0)


@pytest.mark.parametrize("method", ["power", "shin", "multiplicative"])
def test_devig_removes_margin(method):
    odds = [1.6, 4.2, 5.5]
    assert overround(odds) > 0
    p = fair_probs(odds, method)
    assert sum(p) == pytest.approx(1.0)
    assert p[0] > p[1] and all(0 < x < 1 for x in p)


def test_devig_handles_negative_overround():
    p = fair_probs([2.1, 3.9, 4.4], "power")
    assert sum(p) == pytest.approx(1.0)


def test_pool_weights():
    a, b = {"H": 0.5, "D": 0.3, "A": 0.2}, {"H": 0.3, "D": 0.3, "A": 0.4}
    assert pool([a, b], [1.0, 0.0])["H"] == pytest.approx(0.5)
    mid = pool([a, b], [0.5, 0.5])
    assert 0.3 < mid["H"] < 0.5 and sum(mid.values()) == pytest.approx(1.0)


def test_elo_update_and_logit():
    elo = Elo()
    elo.update("A", "B", 3, 0)
    assert elo.rating("A") > 1500 > elo.rating("B")
    p = OrderedLogit().probs(200)
    assert p["H"] > p["A"] and sum(p.values()) == pytest.approx(1.0)
