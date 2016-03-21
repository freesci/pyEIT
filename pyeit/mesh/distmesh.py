# coding: utf-8
# pylint: disable=invalid-name, no-member, no-name-in-module
# pylint: disable=too-many-arguments, too-many-locals
# pylint: disable=too-many-instance-attributes
""" implement 2D/3D distmesh """
from __future__ import absolute_import

from itertools import combinations
import numpy as np
from numpy import sqrt
from scipy.spatial import Delaunay
from scipy.sparse import csr_matrix

from .utils import dist, edge_project


class DISTMESH(object):
    """ class for distmesh """

    def __init__(self, fd, fh, h0=0.1,
                 pfix=None, bbox=None,
                 densityctrlfreq=30,
                 dptol=0.001, ttol=0.1, Fscale=1.2, deltat=0.2):
        """ initial distmesh class

        Parameters
        ----------
        fd : str
            function handle for distance of boundary
        fh : str
            function handle for distance distributions
        h0 : float, optional
            Distance between points in the initial distribution p0, default=0.1
            For uniform meshes, h(x,y) = constant,
            the element size in the final mesh will usually be
            a little larger than this input.
        pfix : array_like, optional
            fixed points, default=[]
        bbox : array_like, optional
            bounding box for region, bbox=[xmin, ymin, xmax, ymax].
            default=[-1, -1, 1, 1]
        densityctrlfreq : int, optional
            cycles of iterations of density control, default=20
        dptol : float, optional
            exit criterion for minimal distance all points moved, default=0.001
        ttol : float, optional
            enter criterion for re-delaunay the lattices, default=0.1
        Fscale : float, optional
            rescaled string forces, default=1.2
            if set too small, points near boundary will be pushed back
            if set too large, points will be pushed towards boundary
        deltat : float, optional
            mapping forces to distances, default=0.2

        """
        # shape description
        self.fd = fd
        self.fh = fh
        self.h0 = h0
        # a small gap, allow points who are slightly outside of the region
        self.geps = 0.001 * h0

        # control the distmesh computation flow
        self.densityctrlfreq = densityctrlfreq
        self.dptol = dptol
        self.ttol = ttol
        self.Fscale = Fscale
        self.deltat = deltat

        # default bbox is 2D
        if bbox is None:
            bbox = [[-1, -1],
                    [1, 1]]
        # p : coordinates (x,y) or (x,y,z) of meshes
        self.Ndim = np.shape(bbox)[1]
        if self.Ndim == 2:
            p = bbox2d(h0, bbox)
        else:
            p = bbox3d(h0, bbox)

        # keep points inside region (specified by fd) with a small gap (geps)
        p = p[fd(p) < self.geps]

        # rejection points by sampling on fh
        r0 = 1. / fh(p)**2
        selection = np.random.rand(p.shape[0]) < (r0 / np.max(r0))
        p = p[selection]

        # specify fixed points
        if pfix is None:
            pfix = []
        self.pfix = pfix
        self.nfix = len(pfix)

        # remove duplicated points of p and pfix
        # avoid overlapping of mesh points
        if len(pfix) > 0:
            p = remove_duplicate_nodes(p, pfix, self.geps)
            p = np.vstack([pfix, p])

        # store p and N
        self.N = p.shape[0]
        self.p = p
        # initialize pold with inf: it will be re-triangulate at start
        self.pold = np.inf * np.ones((self.N, self.Ndim))

        # build edges list for triangle or tetrahedral. i.e., in 2D triangle
        # edge_combinations is [[0, 1], [1, 2], [2, 0]]
        self.edge_combinations = list(combinations(range(self.Ndim+1), 2))
        # triangulate, generate simplices and bars
        self.triangulate()

    def is_retriangulate(self):
        """ test whether re-triangulate is needed """
        return np.max(dist(self.p - self.pold)) > (self.h0 * self.ttol)

    @staticmethod
    def _delaunay(pts, fd, geps):
        """
        Compute the Delaunay triangulation and remove trianges with
        centroids outside the domain (with a geps gap).
        3D, ND compatible

        Parameters
        ----------
        pts : array_like
            points
        fd : str
            distance function
        geps : float
            tol on the gap of distances compared to zero

        Returns
        -------
        array_like
            triangles
        """
        # simplices :
        # triangles where the points are arranged counterclockwise
        tri = Delaunay(pts).simplices
        pmid = np.mean(pts[tri], axis=1)
        # keeps only interior points
        tri = tri[fd(pmid) < -geps]
        return tri

    def triangulate(self):
        """ retriangle by delaunay """
        # pnew[:] = pold[:] makes a new copy, not reference
        self.pold[:] = self.p[:]
        # generate new simplices
        t = self._delaunay(self.p, self.fd, self.geps)
        # extract edges (bars)
        bars = t[:, self.edge_combinations].reshape((-1, 2))
        # sort and remove duplicated edges, eg (1,2) and (2,1)
        # note : for all edges, non-duplicated edge is boundary edge
        bars = np.sort(bars, axis=1)
        # save
        bars_tuple = bars.view([('', bars.dtype)]*bars.shape[1])
        self.bars = np.unique(bars_tuple).view(bars.dtype).reshape((-1, 2))
        self.t = t

    def bar_length(self):
        """ the forces of bars (python is by-default row-wise operation) """
        # two node of a bar
        bars_a, bars_b = self.p[self.bars[:, 0]], self.p[self.bars[:, 1]]
        # bar vector
        barvec = bars_a - bars_b
        # L : length of bars, must be column ndarray (2D)
        L = dist(barvec).reshape((-1, 1))
        # density control on bars
        hbars = self.fh((bars_a + bars_b)/2.0).reshape((-1, 1))
        # L0 : desired lengths (Fscale matters!)
        L0 = hbars * self.Fscale * sqrt(np.sum(L**2) / np.sum(hbars**2))

        return L, L0, barvec

    def bar_force(self, L, L0, barvec):
        """ forces on bars """
        # abs(forces)
        F = np.maximum(L0 - L, 0)
        # normalized and vectorized forces
        Fvec = F * (barvec / L)
        # now, we get forces and sum them up on nodes
        # using sparse matrix to perform automatic summation
        # rows : left, left, right, right (2D)
        #      : left, left, left, right, right, right (3D)
        # cols : x, y, x, y (2D)
        #      : x, y, z, x, y, z (3D)
        data = np.hstack([Fvec, -Fvec])
        if self.Ndim == 2:
            rows = self.bars[:, [0, 0, 1, 1]]
            cols = np.dot(np.ones(np.shape(F)), np.array([[0, 1, 0, 1]]))
        else:
            rows = self.bars[:, [0, 0, 0, 1, 1, 1]]
            cols = np.dot(np.ones(np.shape(F)), np.array([[0, 1, 2, 0, 1, 2]]))
        # sum nodes at duplicated locations using sparse matrices
        Ftot = csr_matrix((data.reshape(-1),
                           [rows.reshape(-1), cols.reshape(-1)]),
                          shape=(self.N, self.Ndim))
        Ftot = Ftot.toarray()
        # zero out forces at fixed points, as they do not move
        Ftot[0:len(self.pfix)] = 0
        return Ftot

    def density_control(self, L, L0):
        """
        Density control - remove points that are too close
        L0 : Kx1, L : Kx1, bars : Kx2
        bars[L0 > 2*L] only returns bar[:, 0] where L0 > 2L
        """
        ixout = (L0 > 2*L).ravel()
        ixdel = np.setdiff1d(self.bars[ixout, :].reshape(-1),
                             np.arange(self.nfix))
        self.p = self.p[np.setdiff1d(np.arange(self.N), ixdel)]
        # Nold = N
        self.N = self.p.shape[0]
        self.pold = np.inf * np.ones((self.N, self.Ndim))
        # print('density control ratio : %f' % (float(N)/Nold))

    def move_p(self, Ftot):
        """ update p """
        # move p along forces
        self.p += self.deltat * Ftot

        # if there is any point ends up outside
        # move it back to the closest point on the boundary
        # using the numerical gradient of distance function
        d = self.fd(self.p)
        ix = d > 0
        if sum(ix) > 0:
            self.p[ix] -= edge_project(self.p[ix], self.fd)

        # check whether convergence : no big movements
        ix_interior = d < -self.geps
        delta_move = self.deltat * Ftot[ix_interior]
        return np.max(dist(delta_move)/self.h0) < self.dptol


def bbox2d(h0, bbox):
    """
    convert bbox to p (not including the ending point of bbox)
    shift every second row h0/2 to the right, therefore,
    all points will be a distance h0 from their closest neighbors

    Parameters
    ----------
    h0 : float
        minimal distance of points
    bbox : array_like
        [[x0, y0],
         [x1, y1]]

    Returns
    -------
    array_like
        points in bbox
    """
    x, y = np.meshgrid(np.arange(bbox[0][0], bbox[1][0], h0),
                       np.arange(bbox[0][1], bbox[1][1], h0*sqrt(3)/2.),
                       indexing='xy')
    # shift even rows of x
    x[1::2, :] += h0/2.
    # p : Nx2 ndarray
    p = np.array([x.ravel(), y.ravel()]).T
    return p


def bbox3d(h0, bbox):
    """ converting bbox to 3D points

    See Also
    --------
    bbox2d : converting bbox to 2D points
    """
    x, y, z = np.meshgrid(np.arange(bbox[0][0], bbox[1][0], h0),
                          np.arange(bbox[0][1], bbox[1][1], h0),
                          np.arange(bbox[0][2], bbox[1][2], h0),
                          indexing='xy')

    p = np.array([x.ravel(), y.ravel(), z.ravel()]).T
    return p


def remove_duplicate_nodes(p, pfix, geps):
    """ remove duplicate points in p who are closed to pfix. 3D, ND compatible

    Parameters
    ----------
    p : array_like
        points in 2D, 3D, ND
    pfix : array_like
        points that are fixed (can not be moved in distmesh)
    geps : float, optional (default=0.01*h0)
        minimal distance that two points are assumed to be identical

    Returns
    -------
    array_like
        non-duplicated points
    """
    for row in pfix:
        pdist = dist(p - row)
        # extract non-duplicated row slices
        p = p[pdist > geps]
    return p


def build(fd, fh, pfix=None,
          bbox=None, h0=0.1, densityctrlfreq=30,
          dptol=0.001, ttol=0.1, Fscale=1.2, deltat=0.2,
          maxiter=500):
    """ main function for distmesh

    See Also
    --------
    DISTMESH : main class for distmesh

    Parameters
    ----------
    maxiter : int, optional
        maximum iteration numbers, default=1000

    Returns
    -------
    p : array_like
        points on 2D bbox
    t : array_like
        triangles describe the mesh structure

    Note
    ----
    there are many python or hybrid python + C implementations in github,
    this implementation is merely implemented from scratch
    using PER-OLOF PERSSON's Ph.D thesis and SIAM paper.

    .. [1] P.-O. Persson, G. Strang, "A Simple Mesh Generator in MATLAB".
       SIAM Review, Volume 46 (2), pp. 329-345, June 2004

    """
    dm = DISTMESH(fd, fh,
                  h0=h0, pfix=pfix, bbox=bbox,
                  densityctrlfreq=densityctrlfreq,
                  dptol=dptol, ttol=ttol, Fscale=Fscale, deltat=deltat)

    # now iterate to push to equilibrium
    for i in range(maxiter):
        if dm.is_retriangulate():
            dm.triangulate()

        # calculate bar forces
        L, L0, barvec = dm.bar_length()

        # density control
        if (i % densityctrlfreq) == 0 and (L0 > 2*L).any():
            dm.density_control(L, L0)
            # continue to triangulate
            continue

        # calculate bar forces
        Ftot = dm.bar_force(L, L0, barvec)

        # update p
        converge = dm.move_p(Ftot)

        # the stopping ctriterion (movements interior are small)
        if converge:
            break

    # at the end of iteration, (p - pold) is small, so we recreate delaunay
    dm.triangulate()

    # you should remove duplicate nodes and triangles
    return dm.p, dm.t