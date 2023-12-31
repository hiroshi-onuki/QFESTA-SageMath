from sage.all import (
    EllipticCurve,
    randint,
    ZZ,
    discrete_log,
    GF,
    matrix,
    block_matrix
)
from sage.modules.free_module_integer import IntegerLattice

from Crypto.Hash import SHAKE256

import quaternion as quat
import endomorphism as End
import elliptic_curve as ec
import parameter_generate as param
import d2isogeny
import supersingular
import compression
import richelot_isogenies as richelot
import utilities

# the image of P, Q under a random isogeny of degree N
def NonSmoothRandomIsog(e, N, basis2, action_matrices, strategy):
    P, Q = basis2
    E0 = P.curve()
    p = E0.base_ring().characteristic()
    assert N % 2 == 1
    assert 2**e > N
    assert (p + 1) % 2**e == 0
    assert ((2**e)*P).is_zero() and ((2**e)*Q).is_zero()
    assert ((2**(e-1))*P).weil_pairing((2**(e-1))*Q, 2) == -1

    alpha = quat.FullRepresentInteger(N*(2**e - N), p)
    vP = End.action_by_matrices(alpha, [1, 0], action_matrices)
    alphaP = vP[0]*P + vP[1]*Q
    vQ = End.action_by_matrices(alpha, [0, 1], action_matrices)
    alphaQ = vQ[0]*P + vQ[1]*Q
 
    assert P.weil_pairing(Q, 2**e)**(N*(2**e - N)) == alphaP.weil_pairing(alphaQ, 2**e)
    X, Y = d2isogeny.D2IsogenyImage(E0, E0, (2**e - N)*P, (2**e - N)*Q, alphaP, alphaQ, e, (P, E0(0)), (Q, E0(0)), strategy)
    Pd, Qd = X[0], Y[0]
    if not Pd.weil_pairing(Qd, 2**e) == P.weil_pairing(Q, 2**e)**N:
        Pd, Qd = X[1], Y[1]
    assert Pd.weil_pairing(Qd, 2**e) == P.weil_pairing(Q, 2**e)**N

    return Pd, Qd

# For an integer matrix M of size (*, 2), return a shortest vector (a, b) satisfying M(a, b)^T = 0 mod N
def shortest_solution_mod(M, N):
    mod_mat = matrix([N]*M.nrows()).transpose()
    M = block_matrix([M, mod_mat], ncols=2)
    K = matrix(M.right_kernel().basis())[:,:2]
    L = IntegerLattice(K)
    return L.shortest_vector()

# return whether the input basis is secure against
# the attack by https://eprint.iacr.org/2023/1433.
def check_basis(basis, N, D, lam, zeta2, Fp4):
    Mj = End.action_matrix([0,-1,2,0], basis, N, zeta2, Fp4)
    Mij = End.action_matrix([-1,0,0,2], basis, N, zeta2, Fp4)

    for idx in [0, 1]:
        P = basis[idx]
        pos = ((idx + 1) % 2, idx)
        sv = shortest_solution_mod(matrix([Mj[pos], Mij[pos]]), N)
        a = ((sv[0]*Mj + sv[1]*Mij) % N)[idx, idx]

        if (N/D**2) * 2**lam > sv[0]**2 + sv[1]**2:
            return False

        # verification
        aP = End.action([-sv[1], -sv[0], 2*sv[0], 2*sv[1]], P, zeta2, Fp4)
        assert aP == a*P

    M = matrix([
        [Mj[1,0], Mij[1,0]],
        [Mj[0,1], Mij[0,1]]
    ])
    sv = shortest_solution_mod(M, N)
    if (N/D)**2 * 2**lam > sv[0]**2 + sv[1]**2:
        return False
    
    T = (sv[0]*Mj + sv[1]*Mij) % N
    a = T[0, 0]
    b = T[1 ,1]

    # verification
    P, Q = basis
    aP = End.action([-sv[1], -sv[0], 2*sv[0], 2*sv[1]], P, zeta2, Fp4)
    bQ = End.action([-sv[1], -sv[0], 2*sv[0], 2*sv[1]], Q, zeta2, Fp4)
    assert aP == a*P and bQ == b*Q

    return True

# OW-PCA PKE
class QFESTA_PKE:
    def __init__(self, lam):
        a, b, f, k, D1, D2 = param.SysParam(lam)
        p = ZZ(2**a*3*f - 1)
        Fp4, Fp2, zeta2 = param.calcFields(p)

        E0 = EllipticCurve(Fp2, [1, 0])
        basis2 = ec.basis(E0, 2, a)

        # if basis2 is weak against the Castryck-Vervcauteren attack, replace another random basis
        is_good_basis = False
        while not is_good_basis:
            basis2 = ec.basis(E0, 2, a)
            is_good_basis = check_basis(basis2, 2**a, min(D1, D2), lam, zeta2, Fp4)

        self.p = p
        self.a = a
        self.b = b
        self.f = f
        self.k = k
        self.D1 = D1
        self.D2 = D2
        self.action_matrices = End.action_matrices(basis2, 2**a, zeta2, Fp4)

        # change base field from Fp2 to Fp2d because Fp2d.gen() should be a square root of -1 for the compression functions
        Fp2d = GF(p**2, modulus=[1, 0, 1], name="i")
        def Fp2ToFp2d(x):
            return ZZ((x + x**p)/2) + ZZ((x - x**p)/(2*zeta2)) * Fp2d.gen()
        E0 = EllipticCurve(Fp2d, [1, 0])
        basis2 = [E0([Fp2ToFp2d(v) for v in P]) for P in basis2]
        self.basis_t2 = basis2
        self.Fp2 = Fp2d

        # pre-computed optimized strategies for richelot chain
        self.strategy = dict()
        for e in [a - 1, a - 2, a - 3, a - 4, a//2]:
            self.strategy[e] = utilities.optimised_strategy(e)

        # for compression
        self.cofactor = ZZ((p + 1) / 2**a)
        self.p_byte_len = (p.nbits() + 7)//8
        self.l_power_byte_len = (a + 7)//8
        self.pk_bytes = 2*self.p_byte_len + 3*self.l_power_byte_len
        self.elligator = supersingular.precompute_elligator_tables(Fp2d)

    def Gen(self):
        basis2 = self.basis_t2
        a = self.a
        D1 = self.D1
        action_matrices = self.action_matrices

        # secret key
        sec_key = 2*randint(0, 2**(a-1)) + 1

        # public key
        Pd, Qd = NonSmoothRandomIsog(a, D1, basis2, action_matrices, self.strategy)

        # transform to a Montgomery curve
        EA, PQ = ec.WeierstrassToMontgomery(Pd.curve(), (2**(a-2)*Pd).xy()[0], [Pd, Qd])
        Pd, Qd = PQ
        Pd = sec_key*Pd
        Qd = ZZ(sec_key).inverse_mod(2**a)*Qd

        # key compression
        pub_key = compression.compress_curve_and_two_torsion_basis(
            EA, Pd, Qd, a, self.elligator, self.cofactor, [],
            self.p_byte_len, self.l_power_byte_len
        )

        return sec_key, pub_key

    def Enc(self, message, pub_key):
        basis2 = self.basis_t2
        a = self.a
        b = self.b
        D1 = self.D1
        D2 = self.D2
        action_matrices = self.action_matrices

        # decompression
        P, Q = basis2
        EA, PA, QA = compression.decompress_curve_and_two_torsion_basis(
            self.Fp2, pub_key, (P, Q, D1), a, self.elligator, self.cofactor, [],
            self.p_byte_len, self.l_power_byte_len
        )

        beta = 2*message + 1
        beta_inv = ZZ(beta).inverse_mod(2**a)

        # isogeny from E0 of degree D2
        P1, Q1 = NonSmoothRandomIsog(a, D2, basis2, action_matrices, self.strategy)

        # isogeny from E1 of degree 3^b
        zeta3 = (-1 + EA.base_ring()(-3).sqrt())/2
        PQA = PA + QA
        E2, xs = ec.chain_3radials(EA, [PA.xy()[0], QA.xy()[0], PQA.xy()[0]], zeta3, b)

        # transform to Montgomery curves.
        # For ProdToJac, 2^(a-1)P1, 2^(a-1)P2 should be (0 0) in the Montgomery curves.
        E1, PQ = ec.WeierstrassToMontgomery(P1.curve(), (2**(a-2)*P1).xy()[0], [P1, Q1])
        P1, Q1 = PQ
        P1, Q1 = beta*P1, beta_inv*Q1
        x4 = xs[0]
        for _ in range(a-2):
            x4 = ec.x_onlyDoubling(E2, x4)
        E2, xs = ec.WeierstrassToMontgomery(E2, x4, xs, x_only=True)
        P2 = E2.lift_x(xs[0])
        Q2 = E2.lift_x(xs[1])
        if not (P2 + Q2).xy()[0] == xs[2]:
            Q2 = -Q2
        P2, Q2 = beta*P2, beta_inv*Q2

        # compression
        ciphertexts = [compression.compress_curve_and_two_torsion_basis(
            Ei, Pi, Qi, a, self.elligator, self.cofactor, [],
            self.p_byte_len, self.l_power_byte_len
        ) for Ei, Pi, Qi in [[E1, P1, Q1], [E2, P2, Q2]]]

        return ciphertexts[0] + ciphertexts[1] 

    def Dec(self, ciphertext, sec_key, pub_key):
        a = self.a
        b = self.b
        k = self.k
        D1 = self.D1
        D2 = self.D2
        P, Q = self.basis_t2

        # decompress public key
        EA, PA, QA = compression.decompress_curve_and_two_torsion_basis(
            self.Fp2, pub_key, (P, Q, D1), a, self.elligator, self.cofactor, [],
            self.p_byte_len, self.l_power_byte_len
        )

        # decompress ciphertext
        l = self.pk_bytes
        E1, P1, Q1 = compression.decompress_curve_and_two_torsion_basis(
            self.Fp2, ciphertext[:l], (P, Q, D2), a, self.elligator, self.cofactor, [],
            self.p_byte_len, self.l_power_byte_len
        )
        E2, P2, Q2 = compression.decompress_curve_and_two_torsion_basis(
            self.Fp2, ciphertext[l:], (PA, QA, 3**b), a, self.elligator, self.cofactor, [],
            self.p_byte_len, self.l_power_byte_len
        )

        # degree check
        if not P2.weil_pairing(Q2, 2**a) == PA.weil_pairing(QA, 2**a)**(3**b):
            return None

        P1d = 3**b*k*P1
        Q1d = 3**b*k*Q1
        P2d = D2*ZZ(sec_key).inverse_mod(2**a)*P2
        Q2d = D2*sec_key*Q2

        assert P1d.weil_pairing(Q1d, 2**a)*P2d.weil_pairing(Q2d, 2**a) == 1
        X, Y = d2isogeny.D2IsogenyImage(E1, E2, P1d, Q1d, P2d, Q2d, a, (E1(0), P2), (E1(0), Q2), self.strategy)
        R, S = X[0], Y[0]
        if not R.curve().is_isomorphic(EA):
            R, S = X[1], Y[1]

        # codomain check
        if not R.curve().is_isomorphic(EA):
            return None

        iota = R.curve().isomorphism_to(EA)
        R, S = iota(R), iota(S)
        m = (ZZ(discrete_log(R, PA, 2**a, operation='+')) * ZZ(3**b*k).inverse_mod(2**a)) % 2**a

        # matrix check
        if not m*S == (3**b*k)*QA:
            return None

        # check that message is constructed by an isogeny from E_0 to E_1
        e = a//2 + 1
        PAd, QAd = 2**(a-e)*PA, 2**(a-e)*QA
        P1d, Q1d = 2**(a-e)*P1, 2**(a-e)*Q1
        PAd, QAd = ZZ(sec_key).inverse_mod(2**e)*PAd, sec_key*QAd
        P1d, Q1d = ZZ(m).inverse_mod(2**e)*P1d, m*Q1d
        assert P1d.weil_pairing(Q1d, 2**e)*PAd.weil_pairing(QAd, 2**e) == 1
        _, codomain = richelot.split_richelot_chain(P1d, Q1d, PAd, QAd, e, self.strategy[e - 1])
        if not (codomain[0].is_isomorphic(P.curve()) or codomain[1].is_isomorphic(P.curve())):
            return None

        if m >= 2**(a-1):
            m = 2**a - m
        return (m - 1)//2

# IND-CCA KEM
class QFESTA_KEM(QFESTA_PKE):
    def __init__(self, lam):
        super().__init__(lam)
        self.m_byte_len = ((self.a - 2) + 7) // 8
        self.n = self.m_byte_len    # byte length of output of Hash function = message length

    def H(self, m):
        shake = SHAKE256.new(m)
        return shake.read(self.n)

    def RandomMessage(self):
        return randint(0, 2**(self.a - 2))

    def Gen(self):
        sk, pk = super().Gen()
        s = self.RandomMessage()
        s = utilities.integer_to_bytes(s)
        return (sk, s), pk

    def Encaps(self, pub_key):
        m = self.RandomMessage()
        c = self.Enc(m, pub_key)
        mb = utilities.integer_to_bytes(m, self.m_byte_len)
        K = self.H(mb + c)
        return K, c
    
    def Decaps(self, ciphertext, sec_key, pub_key):
        sk, s = sec_key
        m = self.Dec(ciphertext, sk, pub_key)
        if m == None:
            return self.H(s + ciphertext)
        else:
            mb = utilities.integer_to_bytes(m, self.m_byte_len)
            return self.H(mb + ciphertext)
